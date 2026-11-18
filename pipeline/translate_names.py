"""
Restaurant names in English / Traditional Chinese / Korean (name_en, name_zh, name_ko) with Claude.

  python translate_names.py estimate              # count targets and estimate cost (no API call)
  python translate_names.py run --limit 10        # normal API, first N untranslated shops (test)
  python translate_names.py batch-submit [--yes]  # Batch API (50% off): estimate, then submit all untranslated shops
  python translate_names.py batch-status
  python translate_names.py batch-collect         # take results into the cache
  python translate_names.py fix [--yes]           # normal API: redo names that still contain Japanese script
  python translate_names.py show [--limit N]      # print cached names
  python translate_names.py apply                 # copy cached names into data/restaurants.geojson

Targets: approved "1" shops in data/restaurants.geojson (test records are not in that file).
State (git-ignored, in ./data): names_cache.json (place_id -> {name_ja, en, zh, ko, usage...}),
names_batch_state.json. name_en / name_zh / name_ko are EDITORIAL_FIELDS in build_restaurants.py,
so a rebuild keeps them. ANTHROPIC_API_KEY is read from the environment or MOP-SSG/project/.env.
"""

import argparse
import collections
import datetime as dt
import json
import os
import re
import sys

import anthropic

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I does not add the script dir
from build_restaurants import DATA, OUT_PATH, load_json, save_json
from generate_descriptions import load_api_key_from_project_env

load_api_key_from_project_env()

MODEL = "claude-haiku-5-5"
PRICE_IN, PRICE_OUT = 0.10 / 1e6, 0.50 / 1e6  # USD per token (Haiku 5.5), same as generate_descriptions.py
CACHE_PATH = os.path.join(DATA, "names_cache.json")
BATCH_STATE_PATH = os.path.join(DATA, "names_batch_state.json")
CONFIRM_ABOVE = 10

SYSTEM = """You translate the names of restaurants, cafes and bars in Okinawa, Japan, for a multilingual travel map site. Readers are English speakers, Taiwanese (Traditional Chinese) and Koreans.

Input: the shop's name as written in Japan (`name`), its genre and its municipality.
Output: the name in English (name_en), Traditional Chinese for Taiwan (name_zh) and Korean (name_ko).

Rules
1. Proper nouns (personal names, coined names, words written in hiragana/katakana with no clear meaning): romanize the Japanese reading in Hepburn without macrons: "とんたん" -> "Tontan", "徳重" -> "Tokushige". In Korean write the Japanese reading in Hangul (도쿠시게, 톤탄). In Chinese keep kanji names in Traditional characters (徳重 -> 德重); a proper noun written in kana stays romanized in Chinese ("ヤフソ" -> "Yafuso", never invented characters such as 亞夫索) unless the shop has a well-known Chinese name.
1b. Keep the order of the parts of the name; translate descriptive parts in place: "炭火焼 一石二鶏" -> "Charcoal Grill Isseki Nicho" / "炭火燒 一石二雞" / "숯불구이 잇세키니초". Wordplay names are romanized as they are read (一石二鶏 is read like 一石二鳥, "isseki nicho").
2. Place names: use the usual name in each language: 沖縄 -> Okinawa / 沖繩 / 오키나와, 那覇 -> Naha / 那霸 / 나하, 国際通り -> Kokusai Street / 國際通 / 국제거리.
3. Words with a clear meaning (food, shop type, ordinary words) are translated: "ステーキハウス" -> "Steak House" / 牛排館 / 스테이크하우스, "食堂" -> "Shokudo" in English only when it is part of a proper name, otherwise "Diner"; "そば" in Okinawa is "Okinawa Soba" / 沖繩麵 / 오키나와 소바 when it means the dish.
4. Brands and chains keep their official names in each language: "マクドナルド" -> "McDonald's" / 麥當勞 / 맥도날드, "スターバックス" -> "Starbucks" / 星巴克 / 스타벅스, "A&W" stays "A&W". Branch suffixes are translated: "那覇店" -> "Naha" (English: "McDonald's Naha"), 那霸店, 나하점.
5. Names already written in Latin letters keep their spelling and styling in all three languages (e.g. "CAFE ZEPHYR", "bar moon"), except that a Japanese part of the name is handled by rules 1-4.
6. English uses Title Case (except names already in Latin letters, rule 5). Do not add words that are not in the name (no "Restaurant" unless the name says so), do not add the municipality unless it is part of the name, no quotes, no explanations.
7. Keep the result short and recognisable: a visitor should be able to match it with the sign of the shop.
8. Never leave Japanese script in the output: no hiragana or katakana in any of the three names, no kanji in name_en or name_ko. Translate or romanize every Japanese part (お菓子屋 -> Okashiya / 甜點店 / 오카시야; イオンモール -> AEON Mall / AEON MALL / 이온몰). The middle dot ・ may stay."""

# Japanese script that must not remain (2026-11-18: 128 of 4,751 batch results kept some)
KANA = re.compile(r"[぀-ヺー-ヿ]")  # hiragana/katakana except the middle dot U+30FB
JAPANESE = re.compile(r"[一-鿿぀-ヺー-ヿ]")


def mixed_script(r):
    """Names that still contain Japanese script where they should not."""
    return [k for k, rx in (("en", JAPANESE), ("zh", KANA), ("ko", JAPANESE)) if rx.search(r.get(k, ""))]

SCHEMA = {
    "type": "object",
    "properties": {
        "name_en": {"type": "string"},
        "name_zh": {"type": "string"},
        "name_ko": {"type": "string"},
    },
    "required": ["name_en", "name_zh", "name_ko"],
    "additionalProperties": False,
}


def targets():
    """(properties) of approved shops, in file order."""
    feats = load_json(OUT_PATH, {"features": []})["features"]
    return [f["properties"] for f in feats if str(f["properties"].get("approved")) == "1"]


def facts(p):
    # Display labels, so the model sees "Steak House" rather than "steak_house"
    from_genre = (p.get("genre") or "").replace("_", " ")
    return {"name": p["title"], "genre": from_genre, "municipality": municipality_en(p)}


def municipality_en(p):
    """English municipality name from the address (same table as the SSG, src/lib/municipalities.ts)."""
    import re
    import unicodedata
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "lib", "municipalities.ts")
    global _MUNI
    if "_MUNI" not in globals():
        src = open(path, encoding="utf-8").read()
        _MUNI = sorted(re.findall(r"^\s+(\S+): L\('([^']+)'", src, re.M), key=lambda x: -len(x[0]))
    a = unicodedata.normalize("NFKC", p.get("address_ja") or p.get("address") or "")
    hits = [(a.find(ja), ja, en) for ja, en in _MUNI if ja in a]
    # drop names that are only part of a longer hit (中城村 in 北中城村, 東村 in 北大東村)
    hits = [h for h in hits if not any(o[1] != h[1] and o[1].endswith(h[1]) and o[0] + len(o[1]) == h[0] + len(h[1]) for o in hits)]
    return min(hits)[2] if hits else ""


def request_params(f):
    return {
        "model": MODEL,
        "max_tokens": 300,
        "system": SYSTEM,
        "thinking": {"type": "disabled"},
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": json.dumps(f, ensure_ascii=False)}],
    }


def parse_message(response, name_ja):
    if response.stop_reason != "end_turn":
        raise RuntimeError(f"stop_reason={response.stop_reason}")
    data = json.loads(next(b.text for b in response.content if b.type == "text"))
    out = {k: data[f"name_{k}"].strip() for k in ("en", "zh", "ko")}
    if not all(out.values()):
        raise ValueError("empty name")
    out.update(name_ja=name_ja, model=response.model, input_tokens=response.usage.input_tokens,
               output_tokens=response.usage.output_tokens,
               generated_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    return out


def is_latin(name):
    """Name written only in Latin letters, digits and ASCII symbols (full-width forms included):
    rule 5 keeps it unchanged in all three languages, so it is copied without an API call (2026-11-18)."""
    import unicodedata
    n = unicodedata.normalize("NFKC", name or "").strip()
    return bool(n) and all(" " <= ch <= "~" for ch in n)


def pending(cache):
    """Approved shops with a non-Latin name and no cached translation of their current name."""
    return [p for p in targets()
            if not is_latin(p["title"]) and cache.get(p["place_id"], {}).get("name_ja") != p["title"]]


def estimate(n, cache):
    measured = [v for v in cache.values() if v.get("input_tokens")]
    if measured:
        avg_in = sum(v["input_tokens"] for v in measured) / len(measured)
        avg_out = sum(v["output_tokens"] for v in measured) / len(measured)
    else:  # system prompt ~650 tokens + input ~40, output ~60 (JSON with three short names)
        avg_in, avg_out = 700, 60
    per = avg_in * PRICE_IN + avg_out * PRICE_OUT
    print(f"{n} shops to {MODEL}; avg/call in {avg_in:.0f}, out {avg_out:.0f} tokens "
          f"({'measured' if measured else 'guess'})")
    print(f"  normal API: ${per * n:.2f}   Batch API (50% off): ${per * n / 2:.2f}")


def cmd_estimate(args):
    cache = load_json(CACHE_PATH, {})
    todo = pending(cache)
    estimate(len(todo), cache)
    for p in todo[:3]:
        print("  example input:", json.dumps(facts(p), ensure_ascii=False))


def cmd_run(args):
    cache = load_json(CACHE_PATH, {})
    todo = pending(cache)
    if args.ids:  # test on chosen shops: --ids okr00035,okr01488
        wanted = set(args.ids.split(","))
        todo = [p for p in targets() if p["pin_id"] in wanted]
    if args.limit:
        todo = todo[: args.limit]
    estimate(len(todo), cache)
    if len(todo) > CONFIRM_ABOVE and not args.yes:
        sys.exit(f"more than {CONFIRM_ABOVE} calls: re-run with --yes after checking the estimate")
    client = anthropic.Anthropic()
    t_in = t_out = 0
    for i, p in enumerate(todo, 1):
        try:
            r = parse_message(client.messages.create(**request_params(facts(p))), p["title"])
        except (RuntimeError, ValueError, KeyError, StopIteration) as e:
            print(f"  {p['pin_id']}: {e}")
            continue
        cache[p["place_id"]] = r
        t_in += r["input_tokens"]
        t_out += r["output_tokens"]
        print(f"  {p['pin_id']} {p['title']} -> {r['en']} | {r['zh']} | {r['ko']}")
        if i % 10 == 0:
            save_json(CACHE_PATH, cache)
    save_json(CACHE_PATH, cache)
    print(f"run: {len(todo)} shops; tokens in {t_in}, out {t_out}, cost ${t_in * PRICE_IN + t_out * PRICE_OUT:.4f}")


def cmd_batch_submit(args):
    cache = load_json(CACHE_PATH, {})
    state = load_json(BATCH_STATE_PATH, {})
    if state.get("batch_id") and not state.get("collected"):
        sys.exit(f"batch {state['batch_id']} is not collected yet: run batch-status / batch-collect first")
    todo = pending(cache)
    if args.limit:
        todo = todo[: args.limit]
    estimate(len(todo), cache)
    if not todo or not args.yes:
        sys.exit("check the estimate above, then re-run with --yes to submit" if todo else "nothing pending")
    requests = [{"custom_id": p["pin_id"], "params": request_params(facts(p))} for p in todo]
    batch = anthropic.Anthropic(timeout=600).messages.batches.create(requests=requests)
    save_json(BATCH_STATE_PATH, {
        "batch_id": batch.id,
        "submitted_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "requests": {p["pin_id"]: {"place_id": p["place_id"], "name_ja": p["title"]} for p in todo},
    })
    print(f"submitted batch {batch.id} ({len(requests)} requests), status {batch.processing_status}")


def cmd_batch_status(args):
    state = load_json(BATCH_STATE_PATH, {})
    if not state.get("batch_id"):
        sys.exit("no batch submitted")
    b = anthropic.Anthropic().messages.batches.retrieve(state["batch_id"])
    c = b.request_counts
    print(f"{b.id}: {b.processing_status} | processing {c.processing}, succeeded {c.succeeded}, "
          f"errored {c.errored}, canceled {c.canceled}, expired {c.expired}"
          f"{' | collected' if state.get('collected') else ''}")


def cmd_batch_collect(args):
    state = load_json(BATCH_STATE_PATH, {})
    if not state.get("batch_id"):
        sys.exit("no batch submitted")
    if state.get("collected"):
        sys.exit("already collected")
    client = anthropic.Anthropic()
    b = client.messages.batches.retrieve(state["batch_id"])
    if b.processing_status != "ended":
        sys.exit(f"batch {b.id} is still {b.processing_status}; try again later")
    cache = load_json(CACHE_PATH, {})
    counts = collections.Counter()
    t_in = t_out = 0
    for r in client.messages.batches.results(b.id):
        entry = state["requests"].get(r.custom_id)
        if not entry:
            continue
        if r.result.type != "succeeded":
            counts[r.result.type] += 1  # stays pending for a later run
            continue
        try:
            res = parse_message(r.result.message, entry["name_ja"])
        except (RuntimeError, ValueError, KeyError, StopIteration):
            counts["unparsable"] += 1
            continue
        res["via_batch"] = True
        cache[entry["place_id"]] = res
        t_in += res["input_tokens"]
        t_out += res["output_tokens"]
        counts["succeeded"] += 1
    save_json(CACHE_PATH, cache)
    state.update(collected=True, batch_cost=round((t_in * PRICE_IN + t_out * PRICE_OUT) / 2, 4))
    save_json(BATCH_STATE_PATH, state)
    print(f"collected {b.id}: {dict(counts)}; tokens in {t_in}, out {t_out}, cost ${state['batch_cost']:.4f} (batch price)")


MAX_FIX_ATTEMPTS = 2


def cmd_fix(args):
    """Regenerate (normal API) cached names that still contain Japanese script; up to 2 tries each."""
    cache = load_json(CACHE_PATH, {})
    by_place = {p["place_id"]: p for p in targets()}
    todo = [pid for pid, v in cache.items() if mixed_script(v) and pid in by_place]
    estimate(len(todo), cache)
    if len(todo) > CONFIRM_ABOVE and not args.yes:
        sys.exit(f"more than {CONFIRM_ABOVE} calls: re-run with --yes after checking the estimate")
    client = anthropic.Anthropic()
    t_in = t_out = 0
    still = []
    for pid in todo:
        p = by_place[pid]
        for attempt in range(MAX_FIX_ATTEMPTS):
            try:
                r = parse_message(client.messages.create(**request_params(facts(p))), p["title"])
            except (RuntimeError, ValueError, KeyError, StopIteration):
                continue
            t_in += r["input_tokens"]
            t_out += r["output_tokens"]
            if not mixed_script(r):
                r["fixed"] = True
                cache[pid] = r
                break
        else:
            still.append(pid)
    save_json(CACHE_PATH, cache)
    print(f"fix: {len(todo) - len(still)}/{len(todo)} fixed; tokens in {t_in}, out {t_out}, "
          f"cost ${t_in * PRICE_IN + t_out * PRICE_OUT:.4f}")
    for pid in still:
        v = cache[pid]
        print(f"  still mixed ({','.join(mixed_script(v))}): {v['name_ja']} -> {v['en']} | {v['zh']} | {v['ko']}")


def cmd_show(args):
    cache = load_json(CACHE_PATH, {})
    for v in list(cache.values())[: args.limit or None]:
        print(f"{v['name_ja']} -> {v['en']} | {v['zh']} | {v['ko']}")


def cmd_apply(args):
    cache = load_json(CACHE_PATH, {})
    geo = load_json(OUT_PATH, {"features": []})
    applied = copied = 0
    for f in geo["features"]:
        p = f["properties"]
        if str(p.get("approved")) != "1":
            continue
        if is_latin(p["title"]):  # rule 5: the same name in every language, no API call
            p["name_en"] = p["name_zh"] = p["name_ko"] = p["title"].strip()
            copied += 1
            continue
        c = cache.get(p["place_id"])
        if c and c.get("name_ja") == p["title"]:  # only when the Japanese name has not changed since
            p["name_en"], p["name_zh"], p["name_ko"] = c["en"], c["zh"], c["ko"]
            applied += 1
    save_json(OUT_PATH, geo, compact=True)
    print(f"apply: translated names on {applied}, Latin names copied on {copied} "
          f"(of {len(geo['features'])} features)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["estimate", "run", "batch-submit", "batch-status", "batch-collect", "fix", "show", "apply"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--ids", help="run: comma-separated pin_ids (test)")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    {"estimate": cmd_estimate, "run": cmd_run, "batch-submit": cmd_batch_submit, "batch-status": cmd_batch_status,
     "batch-collect": cmd_batch_collect, "fix": cmd_fix, "show": cmd_show, "apply": cmd_apply}[args.command](args)


if __name__ == "__main__":
    main()
