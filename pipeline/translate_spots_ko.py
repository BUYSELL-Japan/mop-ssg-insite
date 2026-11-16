"""
Korean names (title_ko) for the MOP spot GeoJSON on S3, with Claude.

  python translate_spots_ko.py fetch                 # download Geojson3.23.geojson from S3 (public URL)
  python translate_spots_ko.py estimate              # pending spots and estimated cost (no API call)
  python translate_spots_ko.py run --limit 10        # translate; more than 10 calls needs --yes
  python translate_spots_ko.py show [--limit N]
  python translate_spots_ko.py build                 # write data/spots_ko/Geojson3.23.geojson and verify the diff
  python translate_spots_ko.py upload --yes          # back up the S3 object, then upload the built file

Scope (decided 2026-11-16): title_ko for every spot (all empty), description_ko only where it is
empty and description_ja exists (oki184). address_ko and all existing Korean text stay untouched.
Files (git-ignored, ./data/spots_ko/): original.geojson, cache.json, Geojson3.23.geojson (build output).
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.request

import anthropic

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I does not add the script dir
from build_restaurants import DATA, load_json, save_json
from generate_descriptions import CONFIRM_ABOVE, PRICE_IN, PRICE_OUT, SAVE_EVERY, KANA, first_segment

BUCKET, KEY = "s3geojsonnew", "Geojson3.23.geojson"
S3_URL = f"https://{BUCKET}.s3.ap-southeast-2.amazonaws.com/{KEY}"
DIR = os.path.join(DATA, "spots_ko")
ORIGINAL_PATH = os.path.join(DIR, "original.geojson")
CACHE_PATH = os.path.join(DIR, "cache.json")
BUILD_PATH = os.path.join(DIR, KEY)
MODEL = "claude-haiku-5-5"
EST_IN, EST_OUT = 900, 60

SYSTEM = """You give Korean names to tourist spots, activities, hotels and restaurants in Okinawa, Japan, for a Korean-language travel map.

You get the Japanese name, the English and Traditional Chinese names, the address, category tags, and sometimes existing Korean text about the place.

Rules for "title_ko":
- If "existing_ko_text" already calls the place by a Korean name, copy that name exactly as it is written there (same spelling and spacing, e.g. "동남 식물 낙원", not a different rendering), without a leading region word like "오키나와" unless it is part of the name. The page shows both, so they must match.
- Otherwise use the name Korean travelers would actually see.
- Proper nouns follow the Japanese reading in Hangul (美ら海 -> 츄라우미, 国際通り -> 국제거리 is fine where that is the established Korean name); generic parts are translated (水族館 -> 수족관, 公園 -> 공원, ビーチ -> 비치, 食堂 -> 식당).
- Brand names written in Latin letters may stay in Latin letters (JUNGLIA OKINAWA, DMM).
- Keep the structure of the original (e.g. "activity: shop name" stays "activity: shop name"). No added words, no explanations, no quotes.
- title_ko is written entirely in Hangul (plus Latin letters, digits and punctuation from the original). Never copy the Japanese name, never leave any kana or kanji, and never add the Japanese original in parentheses. Read kanji place names in Hangul (比謝川 -> 히자강, 百名 -> 햐쿠나) and translate descriptive words (青の洞窟 -> 푸른 동굴, 会場 -> 회장, 体験 -> 체험, 浜 -> 해변).

Rules for "description_ko" (only when "translate_description" is true; otherwise return an empty string):
- Translate description_ja into natural Korean (polite style), keeping its meaning, length and paragraph breaks. Add nothing that is not in the Japanese text.
- Write place names in Hangul; no Japanese kana."""

SCHEMA = {
    "type": "object",
    "properties": {"title_ko": {"type": "string"}, "description_ko": {"type": "string"}},
    "required": ["title_ko", "description_ko"],
    "additionalProperties": False,
}


# Existing fields with wrong content, emptied on build (decided 2026-11-16).
# oki176 創作和食 真-shin-: description_ja / description_ko are King Tacos text; the real content is unknown.
FIELD_CLEARS = {"oki176": ["description_ja", "description_ko"]}


def blank(v):
    return not str(v or "").strip()


def dumps_like_s3(data):
    """The S3 file is json indent=2, ensure_ascii=False, no trailing newline."""
    return json.dumps(data, ensure_ascii=False, indent=2)


def load_original():
    if not os.path.exists(ORIGINAL_PATH):
        sys.exit("run `fetch` first")
    with open(ORIGINAL_PATH, encoding="utf-8") as f:
        return json.load(f)


def cmd_fetch(args):
    os.makedirs(DIR, exist_ok=True)
    req = urllib.request.Request(S3_URL, headers={"User-Agent": "MOPApp/1.0"})
    with urllib.request.urlopen(req, timeout=60) as res:
        raw = res.read()
        etag, modified = res.headers.get("ETag"), res.headers.get("Last-Modified")
    text = raw.decode("utf-8")
    if dumps_like_s3(json.loads(text)) != text:
        sys.exit("S3 file does not round-trip through json.dumps(indent=2); the build would reformat it - stopping")
    with open(ORIGINAL_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    save_json(os.path.join(DIR, "original.meta.json"), {"etag": etag, "last_modified": modified, "bytes": len(raw)})
    print(f"fetched {KEY}: {len(json.loads(text)['features'])} features, {len(raw)} bytes, ETag {etag}, {modified}")


def spot_facts(p):
    translate_desc = blank(p.get("description_ko")) and not blank(p.get("description_ja"))
    existing = (p.get("description_ko") or "").strip()[:200] or first_segment(p.get("meta_title_ko"))
    facts = {
        "name_ja": (p.get("title.ja") or p.get("title.main") or "").strip(),
        "name_en": (p.get("title") or "").strip(),
        "name_zh_tw": (p.get("title_zh_tw") or "").strip(),
        "address_ja": (p.get("address_ja") or "").strip(),
        "tags": (p.get("tags") or "").strip(),
        "existing_ko_text": existing,
        "translate_description": translate_desc,
    }
    if translate_desc:
        facts["description_ja"] = p["description_ja"].strip()
    else:
        facts["description_ja_excerpt"] = (p.get("description_ja") or "").strip()[:150]  # context only
    return {k: v for k, v in facts.items() if v not in ("", None)}


def targets(cache):
    out = []
    for f in load_original()["features"]:
        p = f["properties"]
        if p["pin_id"] not in cache and (blank(p.get("title_ko")) or
                                         (blank(p.get("description_ko")) and not blank(p.get("description_ja")))):
            out.append(p)
    return out


def averages(cache):
    rows = [v for v in cache.values() if v.get("input_tokens")]
    if not rows:
        return EST_IN, EST_OUT, False
    return sum(v["input_tokens"] for v in rows) / len(rows), sum(v["output_tokens"] for v in rows) / len(rows), True


def print_estimate(n, cache):
    a_in, a_out, measured = averages(cache)
    per = a_in * PRICE_IN + a_out * PRICE_OUT
    print(f"pending spots: {n} -> {n} calls to {MODEL}; avg in {a_in:.0f}, out {a_out:.0f} "
          f"({'measured' if measured else 'guess'}); estimated cost ${per * n:.4f}")


def translate(client, facts):
    r = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM,
        thinking={"type": "disabled"},
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": json.dumps(facts, ensure_ascii=False)}],
    )
    if r.stop_reason != "end_turn":
        raise RuntimeError(f"stop_reason={r.stop_reason}")
    data = json.loads(next(b.text for b in r.content if b.type == "text"))
    out = {"title_ko": data["title_ko"].strip(), "input_tokens": r.usage.input_tokens,
           "output_tokens": r.usage.output_tokens,
           "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "input_facts": facts}
    if facts.get("translate_description"):
        out["description_ko"] = data["description_ko"].strip()
    return out


def issues(entry):
    found = []
    if not entry["title_ko"]:
        found.append("empty title_ko")
    if KANA.search(entry["title_ko"]) or any("一" <= ch <= "鿿" for ch in entry["title_ko"]):
        found.append("Japanese script in title_ko")
    if entry["input_facts"].get("translate_description") and not entry.get("description_ko"):
        found.append("empty description_ko")
    return found


def name_mismatch(entry):
    """Note only (not retried): many existing texts never name the place (beach fact lists, activity copy)."""
    existing = entry["input_facts"].get("existing_ko_text", "")
    squash = lambda s: "".join(s.split())
    return bool(existing) and squash(entry["title_ko"]) not in squash(existing)


def cmd_estimate(args):
    cache = load_json(CACHE_PATH, {})
    print_estimate(len(targets(cache)), cache)


def cmd_run(args):
    cache = load_json(CACHE_PATH, {})
    todo = targets(cache)
    if args.pins:
        todo = [p for p in todo if p["pin_id"] in args.pins.split(",")]
    if args.limit:
        todo = todo[: args.limit]
    print_estimate(len(todo), cache)
    if len(todo) > CONFIRM_ABOVE and not args.yes:
        sys.exit(f"more than {CONFIRM_ABOVE} calls: check the estimate, then re-run with --yes")
    client = anthropic.Anthropic()
    t_in = t_out = 0
    for i, p in enumerate(todo, 1):
        facts = spot_facts(p)
        for attempt in (1, 2):  # one retry when the check fails
            try:
                entry = translate(client, facts)
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
                save_json(CACHE_PATH, cache)
                sys.exit(f"stopping: {e}")
            except (anthropic.APIError, RuntimeError, ValueError, KeyError) as e:
                print(f"{p['pin_id']}: failed ({type(e).__name__}: {e})")
                entry = None
                break
            t_in += entry["input_tokens"]
            t_out += entry["output_tokens"]
            problems = issues(entry)
            if not problems:
                break
            print(f"{p['pin_id']}: attempt {attempt}: {problems}")
        if entry:
            if problems:
                entry["check_issues"] = problems
            cache[p["pin_id"]] = entry
        if i % SAVE_EVERY == 0:
            save_json(CACHE_PATH, cache)
    save_json(CACHE_PATH, cache)
    print(f"run: {len(todo)} spots; tokens in {t_in}, out {t_out}, cost ${t_in * PRICE_IN + t_out * PRICE_OUT:.4f}")


def cmd_show(args):
    cache = load_json(CACHE_PATH, {})
    for f in load_original()["features"][: args.limit]:
        p = f["properties"]
        c = cache.get(p["pin_id"])
        if c:
            print(f"{p['pin_id']} [{p.get('category')}] {p.get('title.ja')} | {p.get('title')} -> {c['title_ko']}"
                  f"{'  ISSUES ' + str(c['check_issues']) if c.get('check_issues') else ''}")
            if c.get("description_ko"):
                print(f"  description_ko: {c['description_ko']}")


def cmd_build(args):
    cache = load_json(CACHE_PATH, {})
    original = load_original()
    data = json.loads(json.dumps(original))
    set_title = set_desc = 0
    for f in data["features"]:
        p, c = f["properties"], cache.get(f["properties"]["pin_id"])
        if not c:
            continue
        if blank(p.get("title_ko")):
            p["title_ko"] = c["title_ko"]
            set_title += 1
        if c.get("description_ko") and blank(p.get("description_ko")):
            p["description_ko"] = c["description_ko"]
            set_desc += 1
    cleared = []
    for f in data["features"]:
        for k in FIELD_CLEARS.get(f["properties"]["pin_id"], []):
            f["properties"][k] = ""
            cleared.append(f"{f['properties']['pin_id']}.{k}")
    # Verify: nothing but empty title_ko / description_ko changed
    assert len(data["features"]) == len(original["features"])
    for new, old in zip(data["features"], original["features"]):
        assert new["geometry"] == old["geometry"] and new.keys() == old.keys()
        assert new["properties"].keys() == old["properties"].keys()
        for k, v in new["properties"].items():
            if v != old["properties"][k]:
                pin = old["properties"]["pin_id"]
                if k in FIELD_CLEARS.get(pin, []):
                    assert v == "", (pin, k)
                    continue
                assert k in ("title_ko", "description_ko") and blank(old["properties"][k]), (pin, k)
    os.makedirs(DIR, exist_ok=True)
    with open(BUILD_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(dumps_like_s3(data))
    missing = sum(1 for f in data["features"] if blank(f["properties"].get("title_ko")))
    print(f"build: title_ko set on {set_title}, description_ko on {set_desc}; cleared {cleared}; still empty title_ko: {missing}; "
          f"verified: no other field changed -> {BUILD_PATH}")


def cmd_upload(args):
    if not os.path.exists(BUILD_PATH):
        sys.exit("run `build` first")
    meta = load_json(os.path.join(DIR, "original.meta.json"), {})
    head = json.loads(subprocess.run(["aws", "s3api", "head-object", "--bucket", BUCKET, "--key", KEY],
                                     capture_output=True, text=True, check=True).stdout)
    if head["ETag"] != meta.get("etag"):
        sys.exit(f"S3 object changed since fetch ({head['ETag']} != {meta.get('etag')}): run fetch/build again")
    backup = f"backup/{KEY}.{dt.datetime.now():%Y%m%d-%H%M%S}"
    print(f"will copy s3://{BUCKET}/{KEY} -> s3://{BUCKET}/{backup}, then upload {BUILD_PATH}")
    if not args.yes:
        sys.exit("re-run with --yes to upload")
    subprocess.run(["aws", "s3", "cp", f"s3://{BUCKET}/{KEY}", f"s3://{BUCKET}/{backup}"], check=True)
    subprocess.run(["aws", "s3", "cp", BUILD_PATH, f"s3://{BUCKET}/{KEY}",
                    "--content-type", head.get("ContentType", "application/json")], check=True)
    print(f"uploaded; backup at s3://{BUCKET}/{backup}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("fetch", "estimate", "build"):
        sub.add_parser(name)
    run = sub.add_parser("run")
    run.add_argument("--limit", type=int)
    run.add_argument("--yes", action="store_true")
    run.add_argument("--pins", help="comma-separated pin_ids to translate (e.g. oki184)")
    show = sub.add_parser("show")
    show.add_argument("--limit", type=int, default=264)
    up = sub.add_parser("upload")
    up.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    {"fetch": cmd_fetch, "estimate": cmd_estimate, "run": cmd_run, "show": cmd_show,
     "build": cmd_build, "upload": cmd_upload}[args.cmd](args)


if __name__ == "__main__":
    main()
