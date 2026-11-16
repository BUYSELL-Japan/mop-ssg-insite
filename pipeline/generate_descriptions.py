"""
Restaurant descriptions (en / zh_tw / ko) with Claude, one API call per restaurant.

  python generate_descriptions.py estimate            # count pending rows and estimate cost (no API call)
  python generate_descriptions.py run --limit 10      # generate for the first 10 pending rows
  python generate_descriptions.py run --yes           # all pending rows (more than 10 needs --yes)
  python generate_descriptions.py show [--limit N]    # print cached descriptions
  python generate_descriptions.py apply               # copy cached descriptions into data/restaurants.geojson
  python generate_descriptions.py batch-submit [--yes] # Batch API (50% off): estimate, then submit all pending rows
  python generate_descriptions.py batch-status        # processing / succeeded / errored counts
  python generate_descriptions.py batch-collect       # take results, regenerate failed checks (normal API), apply

State (git-ignored, in ./data):
  descriptions_cache.json   place_id -> {en, zh_tw, ko, usage...}; saved every 10 rows, so a run can resume
Reads data/restaurants.geojson (build output) and data/restaurants.csv (for 検索エリア).
`apply` only fills description_* fields; approved stays "0" until a person reviews the text.
ANTHROPIC_API_KEY is read from the environment or pipeline/.env.
"""

import argparse
import collections
import datetime as dt
import json
import math
import os
import re
import urllib.request
import random
import sys
import time
import unicodedata

import anthropic

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I does not add the script dir
from build_restaurants import DATA, OUT_PATH, load_json, read_rows, save_json

CACHE_PATH = os.path.join(DATA, "descriptions_cache.json")


def load_api_key_from_project_env():
    """Fallback: take only ANTHROPIC_API_KEY from MOP-SSG/project/.env (git-ignored) if not set yet."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.environ.get("ANTHROPIC_API_KEY") or not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() == "ANTHROPIC_API_KEY":
                os.environ["ANTHROPIC_API_KEY"] = value.strip().strip('"').strip("'")


load_api_key_from_project_env()

MODEL = "claude-haiku-5-5"
PRICE_IN, PRICE_OUT = 0.10 / 1e6, 0.50 / 1e6  # USD per token (Haiku 5.5, prompts up to 100K tokens)
SAVE_EVERY = 10
CONFIRM_ABOVE = 10  # runs larger than this need --yes (external API: estimate first, then run)

SYSTEM = """You write short introductions of restaurants in Okinawa, Japan, for a multilingual travel map site (English, Traditional Chinese for Taiwan, Korean).

You get a few facts about one restaurant. Write 2-3 sentences per language based only on those facts.

Rules:
- Do not write anything that is not in the input. Do not invent dishes, menu items, history, owners, interior, views, parking or awards. If the genre is generic, stay generic rather than guessing.
- Do not describe what the place is good for: no lunch, dinner, light meal, snack, date, family or similar uses or occasions.
- "reputation", when present, is one of "highly popular", "well-reviewed", "well-regarded"; express it in natural words. If it is absent, say nothing about reputation. Never mention ratings, review counts, Google or any review site.
- "genre_terms", when present, gives the exact wording for the genre in each language (en, zh_tw, ko); use it as given.
- "landmark", when present, gives the nearest well-known sight and how far it is ("near" or "about N km from"). Mention it once, with exactly that distance wording (never a more precise number), using the landmark name given for each language (names.en / names.zh_tw / names.ko). If there is no "landmark", do not mention any sight or distance.
- Write distances with "km" in every language (Chinese: 約10km, never 公里; Korean: 약 10km). Korean text must not contain English words.
- You may mention the area and the price level in words. Do not list opening times; the page shows them separately. You may mention a notable pattern in the hours, such as open on weekends only or closed on certain days.
- Start the first sentence the way "opening" says: "area" = start from the area; "genre" = start from what kind of restaurant it is; "price" = start from the price level; "hours" = start from a notable feature of its opening days. Use the same opening in all three languages.
- Write the restaurant name as given (Japanese script is fine); you may add a short reading or translation.
- Place names (city, town, island, area) must match the language: in Traditional Chinese, never use Japanese hiragana or katakana; write the place name in kanji (うるま市 -> 宇流麻市, 那覇市 -> 那霸市). In Korean, write place names in Hangul by their Japanese reading (うるま市 -> 우루마시, 那覇市 -> 나하시).
- Each language is written natively, not as a literal translation of the English: natural English for travelers; Traditional Chinese (繁體中文) as used in Taiwan; natural Korean (polite style).
- No marketing exaggeration, no emojis, no headings, plain sentences only."""

SCHEMA = {
    "type": "object",
    "properties": {
        "description_en": {"type": "string"},
        "description_zh_tw": {"type": "string"},
        "description_ko": {"type": "string"},
    },
    "required": ["description_en", "description_zh_tw", "description_ko"],
    "additionalProperties": False,
}

# Used by `estimate` before any real usage exists; replaced by measured averages once the cache has rows.
EST_IN_TOKENS, EST_OUT_TOKENS = 700, 700


def reputation(rating, reviews):
    """Raw numbers never reach the model, so they cannot end up in the text."""
    rating, reviews = rating or 0, reviews or 0
    if rating >= 4.5 and reviews >= 300:
        return "highly popular"
    if rating >= 4.0 and reviews >= 100:
        return "well-reviewed"
    if rating >= 4.0:
        return "well-regarded"
    return None


def has_notable_hours(osm):
    """True when the days are restricted (some day is off, or not every day is listed)."""
    if not osm:
        return False
    return " off" in osm or not any(d in osm for d in ("Mo-Su", "24/7"))


def opening_style(p, area, facts):
    """Pick the first-sentence pattern; seeded by place_id so a rerun gives the same choice."""
    options = ["genre"]
    if area:
        options.append("area")
    if facts.get("price_range"):
        options.append("price")
    if has_notable_hours(facts.get("opening_hours")):
        options.append("hours")
    return random.Random(p["place_id"]).choice(options)


# ------------------------------------------------------------------ landmarks

SPOTS_URL = "https://s3geojsonnew.s3.ap-southeast-2.amazonaws.com/Geojson3.23.geojson"
SPOTS_PATH = os.path.join(DATA, "spots.geojson")  # local copy of the MOP spot data (read-only use)
# Only sights most foreign visitors know (decided 2026-11-16). Five come from the spot data...
LANDMARK_PIN_IDS = {"oki01", "oki02", "oki05", "oki07", "oki214"}  # Churaumi, Kokusai St, Shuri Castle, American Village, Naha Airport
# Names the spot data lacks (title_ko and meta_title_ko both empty or unusable)
LANDMARK_NAME_OVERRIDES = {
    "oki214": {"ko": "나하 공항"},
}
# ...and three are not in it, so their coordinates are fixed here (public map coordinates)
EXTRA_LANDMARKS = [
    {"pin_id": "x-kouri", "lat": 26.7050, "lng": 128.0180, "en": "Kouri Island", "zh_tw": "古宇利島", "ko": "고우리섬"},
    {"pin_id": "x-manzamo", "lat": 26.5049, "lng": 127.8506, "en": "Cape Manzamo", "zh_tw": "萬座毛", "ko": "만자모"},
    {"pin_id": "x-peace", "lat": 26.0957, "lng": 127.7264, "en": "Okinawa Peace Memorial Park",
     "zh_tw": "和平祈念公園", "ko": "평화기념공원"},
]
NEAR_KM, MAX_KM = 3, 20


def first_segment(meta_title):
    """'슈리성 공원｜역사와...｜MOP' -> '슈리성 공원' (only when it really is a 'name｜tagline' title)."""
    return meta_title.split("｜")[0].strip() if meta_title and "｜" in meta_title else ""


def load_landmarks():
    if not os.path.exists(SPOTS_PATH):
        req = urllib.request.Request(SPOTS_URL, headers={"User-Agent": "MOPApp/1.0"})
        with urllib.request.urlopen(req, timeout=60) as res:
            data = json.loads(res.read())
        save_json(SPOTS_PATH, data, compact=True)
    landmarks = []
    for f in load_json(SPOTS_PATH, {})["features"]:
        p = f["properties"]
        if p.get("pin_id") not in LANDMARK_PIN_IDS or not f.get("geometry"):
            continue
        override = LANDMARK_NAME_OVERRIDES.get(p.get("pin_id"), {})
        zh = re.sub(r"門票$", "", (p.get("title_zh_tw") or "").strip())  # e.g. 沖繩兒童王國門票 (ticket listing title)
        lng, lat = f["geometry"]["coordinates"][:2]
        landmarks.append({
            "pin_id": p.get("pin_id"),
            "lat": lat, "lng": lng,
            "en": p["title"].strip(),
            "zh_tw": override.get("zh_tw") or zh or p["title"].strip(),
            "ko": override.get("ko") or (p.get("title_ko") or "").strip()
                  or first_segment(p.get("meta_title_ko")) or p["title"].strip(),
        })
    return landmarks + EXTRA_LANDMARKS


def distance_km(lat1, lng1, lat2, lng2):
    """Great-circle (haversine) distance."""
    dlat, dlng = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    h = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def nearest_landmark(feature, landmarks):
    """Rounded on purpose: most coordinates are town-level and can be off by a few km."""
    if not feature.get("geometry"):
        return None
    lng, lat = feature["geometry"]["coordinates"][:2]
    km, lm = min(((distance_km(lat, lng, m["lat"], m["lng"]), m) for m in landmarks), key=lambda x: x[0])
    if km > MAX_KM:
        return None
    names = {"en": lm["en"], "zh_tw": lm["zh_tw"], "ko": lm["ko"]}
    if km <= NEAR_KM:
        return {"relation": "near", "names": names, "_km": round(km, 1)}
    rounded = max(5, int(km / 5 + 0.5) * 5)  # 5 km steps: 3-7.4 -> 5, 7.5-12.4 -> 10, ...
    return {"relation": f"about {rounded} km from", "names": names, "_km": round(km, 1)}


# ------------------------------------------------------------------ genres

# Wording the model must use for codes that are easy to mistranslate
GENRE_TERMS = {
    "snack_bar": {"en": "snack bar (casual dining)", "zh_tw": "小吃店", "ko": "스낵바"},
}


def restaurant_facts(p, row, feature=None, landmarks=None):
    area = row.get("検索エリア", "").strip()
    codes = [c.strip() for c in row.get("業態分類", "").split("/") if c.strip()] or [p.get("genre") or ""]
    genre = codes[0].replace("_", " ")
    facts = {
        "name": p.get("title_ja"),
        "genre": genre or None,
        "genre_terms": GENRE_TERMS.get(codes[0]),
        "area": area or None,
        "reputation": reputation(p.get("rating"), p.get("review_count")),
        "price_range": p.get("price_range"),
        "opening_hours": p.get("opening_hours"),
    }
    lm = nearest_landmark(feature, landmarks) if feature is not None and landmarks else None
    if lm:
        facts["landmark"] = {k: v for k, v in lm.items() if not k.startswith("_")}
    facts = {k: v for k, v in facts.items() if v not in (None, "")}
    facts["opening"] = opening_style(p, area, facts)
    return facts


def load_targets():
    """[(properties, csv_row, feature)] in GeoJSON order."""
    features = load_json(OUT_PATH, {"features": []})["features"]
    if not features:
        sys.exit(f"no features in {OUT_PATH}; run build_restaurants.py build first")
    rows = {r["place_id"].strip(): r for r in read_rows()}
    return [(f["properties"], rows.get(f["properties"]["place_id"], {}), f) for f in features]


def pending(targets, cache):
    return [t for t in targets if t[0]["place_id"] not in cache]


def averages(cache):
    rows = [v for v in cache.values() if v.get("input_tokens")]
    if not rows:
        return EST_IN_TOKENS, EST_OUT_TOKENS, False
    calls = sum(v.get("attempts", 1) for v in rows)  # per API call, not per restaurant
    return (sum(v["input_tokens"] for v in rows) / calls,
            sum(v["output_tokens"] for v in rows) / calls, True)


def print_estimate(n, cache):
    avg_in, avg_out, measured = averages(cache)
    per = avg_in * PRICE_IN + avg_out * PRICE_OUT
    basis = f"measured over {len(cache)} cached rows" if measured else "rough guess, no usage measured yet"
    print(f"pending rows: {n}  -> {n} API calls to {MODEL}")
    print(f"avg tokens/call: in {avg_in:.0f}, out {avg_out:.0f} ({basis})")
    print(f"estimated cost: ${per * n:.2f} (${per:.5f}/call); Batch API (50% off): ${per * n / 2:.2f}")


def request_params(facts):
    """Same request for the normal API and for a Batch API entry."""
    return {
        "model": MODEL,
        "max_tokens": 1500,
        "system": SYSTEM,
        "thinking": {"type": "disabled"},  # accepted on Haiku 5.5 at effort high or below
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": json.dumps(facts, ensure_ascii=False)}],
    }


def generate(client, facts):
    return parse_message(client.messages.create(**request_params(facts)))


def parse_message(response):
    if response.stop_reason != "end_turn":
        # refusal (no server-side fallback on Haiku) or max_tokens: leave uncached so it is retried
        raise RuntimeError(f"stop_reason={response.stop_reason}")
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    return {
        "en": data["description_en"].strip(),
        "zh_tw": data["description_zh_tw"].strip(),
        "ko": data["description_ko"].strip(),
        "model": response.model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }


# ------------------------------------------------------------------ checks after generation

MAX_ATTEMPTS = 3  # first try + 2 regenerations when a check fails
KANA = re.compile(r"[぀-ヿ]")
LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
CLOCK_TIME = re.compile(r"\d{1,2}:\d{2}")
HOUR_TEXT = {  # clock times written in words (2026-11-16: HH:MM alone missed 오전 9시 / 上午9點 / 9 a.m.)
    "en": re.compile(r"\b\d{1,2}\s?(?:a\.?m\b\.?|p\.?m\b\.?)|\b(?:noon|midnight)\b", re.I),
    "zh_tw": re.compile(r"\d{1,2}\s?(?:點|点|時(?!間))"),
    "ko": re.compile(r"\d{1,2}\s?시(?!간)|정오|자정"),
}
META_TEXT = re.compile(  # prompt / tool text that leaked into a description
    r"</?[A-Za-z_]+>|reasoning|(?-i:\binfo[A-Z])|\bfacts\b|basic (?:facts|information)|확인된 기본 정보|基本資訊", re.I)


def has_time(lang, text):
    return bool(CLOCK_TIME.search(text) or HOUR_TEXT[lang].search(text))
USE_WORDS = {  # uses/occasions the prompt forbids
    "en": re.compile(r"\b(lunch|dinner|breakfast|light meal|date night)\b", re.I),
    "zh_tw": re.compile(r"午餐|晚餐|早餐|輕食"),
    "ko": re.compile(r"점심|저녁 식사|아침 식사|가벼운 식사"),
}
KM_TEXT = re.compile(r"(\d+)\s*(?:公里|킬로미터|㎞|km|KM|Km)")


def normalize_km(result):
    """One distance notation: en 'about 10 km', zh '約10km', ko '약 10km' (no 公里 / 킬로미터 / ㎞)."""
    result["en"] = KM_TEXT.sub(r"\1 km", result["en"])
    for lang in ("zh_tw", "ko"):
        result[lang] = KM_TEXT.sub(r"\1km", result[lang])


NAME_SEPARATORS = re.compile(r"[\s\[\]（）()【】「」・/|_]+")


def strip_known_names(facts, lang, text):
    """Remove the shop name, each part of it, and the landmark name. The model often keeps only
    part of the name or rewrites part of it (那覇 -> 那霸), so the parts must count as name too."""
    name = facts.get("name") or ""
    parts = [x for x in NAME_SEPARATORS.split(name) if x]
    for x in sorted(set(parts + [name]) - {""}, key=len, reverse=True):
        text = text.replace(x, "")
    lm = facts.get("landmark")
    if lm:
        text = text.replace(lm["names"][lang], "")
    return text


ENGLISH_PLAIN = {"about", "from", "near", "the", "is", "and", "in", "of", "with", "it", "its", "this", "located",
                 "open", "closed", "prices", "price", "okinawa", "japanese", "restaurant"}


def english_name(en_text):
    """Subject of the first English sentence, i.e. how the English text names the shop."""
    first = re.split(r"(?<=[.!?])\s+", en_text)[0]
    m = re.match(r"(?:Located in [^,]+, |Open [^,]+, |Closed [^,]+, )?(.{2,80}?) (?:is|serves|offers|sits)\b", first)
    return m.group(1) if m else ""


def check_description(facts, result):
    """Problems a person would otherwise have to catch by eye; empty list = ok."""
    issues = []
    name = facts.get("name") or ""
    lm = facts.get("landmark")
    m = re.search(r"(\d+) km", lm["relation"]) if lm else None
    km = m.group(1) if m else None
    for lang in ("en", "zh_tw", "ko"):
        text = result[lang]
        rest = strip_known_names(facts, lang, text)
        if KANA.search(rest):
            issues.append(f"{lang}: kana outside the name")
        if has_time(lang, text):
            issues.append(f"{lang}: opening times written out")
        if META_TEXT.search(text):
            issues.append(f"{lang}: prompt or tool text")
        if USE_WORDS[lang].search(text):
            issues.append(f"{lang}: meal-use word")
        if lang == "ko":
            # Latin inside the shop name is fine, also when the name is full-width (ＲＩＭ) or katakana
            # rendered in English (チョコレートゲイブルズ -> Chocolate Gables, taken from the English text)
            allowed = {"km"} | {w.lower() for w in LATIN_WORD.findall(unicodedata.normalize("NFKC", name))}
            allowed |= {w.lower() for w in re.findall("[A-Z][a-z]+", unicodedata.normalize("NFKC", name))}  # NewClubL -> New, Club
            en_texts = " ".join([result.get("en") or "", (result.get("before_cleanup") or {}).get("en", "")])
            allowed |= {w.lower() for w in LATIN_WORD.findall(english_name(en_texts))}
            # proper nouns of the English text (Chocolate Gables, Subway, AEON Mall); never plain words like "about"
            allowed |= {w.lower() for w in re.findall(r"\b[A-Z][A-Za-z]+", en_texts)} - ENGLISH_PLAIN
            words = [w for w in LATIN_WORD.findall(unicodedata.normalize("NFKC", text)) if w.lower() not in allowed]
            if words:
                issues.append(f"ko: English words {words[:3]}")
        numbers = set(KM_TEXT.findall(text))
        if km and km not in numbers:
            issues.append(f"{lang}: distance {km} km missing")
        if numbers - ({km} if km else set()):
            issues.append(f"{lang}: unexpected distance {sorted(numbers)}")
        if lm and lm["names"][lang] not in text:
            issues.append(f"{lang}: landmark name missing")
    return issues


def generate_checked(client, facts, previous=None):
    """Regenerate only this restaurant while a check fails; tokens of every attempt are counted.

    `previous` is an earlier result (e.g. from the batch) that failed the checks; its attempts count too."""
    attempts = previous.get("attempts", 1) if previous else 0
    tokens_in = previous["input_tokens"] if previous else 0
    tokens_out = previous["output_tokens"] if previous else 0
    result = previous
    issues = check_description(facts, previous) if previous else ["not generated"]
    while issues and attempts < MAX_ATTEMPTS:
        attempts += 1
        result = generate(client, facts)
        tokens_in += result["input_tokens"]
        tokens_out += result["output_tokens"]
        normalize_km(result)
        issues = check_description(facts, result)
        if issues:
            print(f"  attempt {attempt_label(attempts)}: {'; '.join(issues)}")
    result = dict(result, input_tokens=tokens_in, output_tokens=tokens_out, attempts=attempts)
    if previous and previous.get("via_batch"):
        result["via_batch"] = True  # first attempt was billed at the batch price
    result.pop("check_issues", None)
    if issues:
        result["check_issues"] = issues  # kept, but flagged for review
    return result


def attempt_label(n):
    return f"{n}/{MAX_ATTEMPTS}"


def cmd_estimate(args):
    cache = load_json(CACHE_PATH, {})
    print_estimate(len(pending(load_targets(), cache)), cache)


def cmd_run(args):
    cache = load_json(CACHE_PATH, {})
    todo = pending(load_targets(), cache)
    if args.limit:
        todo = todo[:args.limit]
    print_estimate(len(todo), cache)
    if len(todo) > CONFIRM_ABOVE and not args.yes:
        sys.exit(f"more than {CONFIRM_ABOVE} calls: check the estimate above, then re-run with --yes")
    if not todo:
        return

    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY from env / .env (loaded by build_restaurants)
    done = failed = 0
    tokens_in = tokens_out = 0
    landmarks = load_landmarks()
    for i, (p, row, feature) in enumerate(todo, 1):
        try:
            facts = restaurant_facts(p, row, feature, landmarks)
            cache[p["place_id"]] = result = generate_checked(client, facts)
            result["input_facts"] = facts
            done += 1
            tokens_in += result["input_tokens"]
            tokens_out += result["output_tokens"]
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as e:
            save_json(CACHE_PATH, cache)
            sys.exit(f"stopping: {type(e).__name__}: {e}")
        except anthropic.RateLimitError:
            print(f"{p['pin_id']}: rate limited after SDK retries, waiting 60s")
            time.sleep(60)
            failed += 1
        except (anthropic.APIError, RuntimeError, ValueError, KeyError) as e:
            print(f"{p['pin_id']}: failed ({type(e).__name__}: {e})")
            failed += 1
        if i % SAVE_EVERY == 0:
            save_json(CACHE_PATH, cache)
        if i % 100 == 0:
            print(f"{i}/{len(todo)} done ({failed} failed)")
    save_json(CACHE_PATH, cache)
    cost = tokens_in * PRICE_IN + tokens_out * PRICE_OUT
    print(f"run: {done} generated, {failed} failed (retried on next run); "
          f"tokens in {tokens_in}, out {tokens_out}, cost ${cost:.4f}")


def cmd_show(args):
    cache = load_json(CACHE_PATH, {})
    for p, row, _ in load_targets():
        area = row.get("検索エリア", "")
        c = cache.get(p["place_id"])
        if not c:
            continue
        print(f"--- {p['pin_id']} {p['title_ja']} | {p.get('genre')} | {area} | "
              f"{p.get('rating')} ({p.get('review_count')}) | {p.get('price_range')}")
        print(f"EN: {c['en']}\nZH: {c['zh_tw']}\nKO: {c['ko']}")
        flag = f", ISSUES {c['check_issues']}" if c.get("check_issues") else ""
        print(f"   attempts {c.get('attempts', 1)}, tokens in {c['input_tokens']} out {c['output_tokens']}{flag}\n")
        args.limit -= 1
        if args.limit <= 0:
            break


def cmd_apply(args):
    cache = load_json(CACHE_PATH, {})
    geo = load_json(OUT_PATH, {"features": []})
    applied = 0
    for f in geo["features"]:
        p = f["properties"]
        c = cache.get(p["place_id"])
        if c:
            p["description_en"], p["description_zh_tw"], p["description_ko"] = c["en"], c["zh_tw"], c["ko"]
            applied += 1
    save_json(OUT_PATH, geo, compact=True)
    print(f"apply: descriptions set on {applied}/{len(geo['features'])} features (approved unchanged)")


# ------------------------------------------------------------------ Batch API (50% off, results within 24 h)

BATCH_STATE_PATH = os.path.join(DATA, "batch_state.json")  # {batch_id, submitted_at, requests: {pin_id: {place_id, facts}}}
RETRY_SHARE = 0.2  # share of rows expected to fail the checks (2 of 10 in the 2026-11-16 test)


def recheck_targets(cache):
    """Cached rows that fail the current checks and still have attempts left."""
    return [pid for pid, v in cache.items()
            if v.get("input_facts") and v.get("attempts", 1) < MAX_ATTEMPTS
            and check_description(v["input_facts"], v)]


def cmd_batch_submit(args):
    cache = load_json(CACHE_PATH, {})
    state = load_json(BATCH_STATE_PATH, {})
    if state.get("batch_id") and not state.get("collected"):
        sys.exit(f"batch {state['batch_id']} is not collected yet: run batch-status / batch-collect first")
    todo = pending(load_targets(), cache)
    if args.limit:
        todo = todo[:args.limit]
    avg_in, avg_out, measured = averages(cache)
    per = avg_in * PRICE_IN + avg_out * PRICE_OUT
    retries = len(todo) * RETRY_SHARE + len(recheck_targets(cache))
    print(f"batch: {len(todo)} requests to {MODEL} (avg/call in {avg_in:.0f}, out {avg_out:.0f}, "
          f"{'measured' if measured else 'guess'})")
    print(f"  batch cost (50% off):           ${per * len(todo) / 2:.2f}")
    print(f"  retries via normal API, ~{retries:.0f} rows x up to {MAX_ATTEMPTS - 1} calls: "
          f"${per * retries:.2f} - ${per * retries * (MAX_ATTEMPTS - 1):.2f}")
    print(f"  total:                          ${per * len(todo) / 2 + per * retries:.2f}"
          f" - ${per * len(todo) / 2 + per * retries * (MAX_ATTEMPTS - 1):.2f}")
    if not todo or not args.yes:
        sys.exit("check the estimate above, then re-run with --yes to submit" if todo else "nothing pending")

    landmarks = load_landmarks()
    requests, entries = [], {}
    for p, row, feature in todo:
        facts = restaurant_facts(p, row, feature, landmarks)
        entries[p["pin_id"]] = {"place_id": p["place_id"], "facts": facts}
        requests.append({"custom_id": p["pin_id"], "params": request_params(facts)})
    client = anthropic.Anthropic(timeout=600)
    batch = client.messages.batches.create(requests=requests)
    save_json(BATCH_STATE_PATH, {
        "batch_id": batch.id,
        "submitted_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "requests": entries,
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
    """Take batch results into the cache, regenerate failed checks with the normal API, then apply."""
    state = load_json(BATCH_STATE_PATH, {})
    if not state.get("batch_id"):
        sys.exit("no batch submitted")
    client = anthropic.Anthropic()
    if not state.get("collected"):
        b = client.messages.batches.retrieve(state["batch_id"])
        if b.processing_status != "ended":
            sys.exit(f"batch {b.id} is still {b.processing_status}; try again later")
        cache = load_json(CACHE_PATH, {})
        counts = collections.Counter()
        tokens_in = tokens_out = 0
        for r in client.messages.batches.results(b.id):
            entry = state["requests"].get(r.custom_id)
            if not entry:
                continue
            if r.result.type != "succeeded":
                counts[r.result.type] += 1  # left out of the cache -> pending for a later run
                continue
            try:
                result = parse_message(r.result.message)
            except (RuntimeError, ValueError, KeyError, StopIteration):
                counts["unparsable"] += 1
                continue
            normalize_km(result)
            result.update(input_facts=entry["facts"], attempts=1, via_batch=True)
            tokens_in += result["input_tokens"]
            tokens_out += result["output_tokens"]
            cache[entry["place_id"]] = result
            counts["succeeded"] += 1
        save_json(CACHE_PATH, cache)
        state["collected"] = True
        state["batch_cost"] = round((tokens_in * PRICE_IN + tokens_out * PRICE_OUT) / 2, 4)
        save_json(BATCH_STATE_PATH, state)
        print(f"collected {b.id}: {dict(counts)}; tokens in {tokens_in}, out {tokens_out}, "
              f"cost ${state['batch_cost']:.4f} (batch price)")

    # Regenerate only rows that fail the checks (normal API); resumable, saved every 10 rows
    cache = load_json(CACHE_PATH, {})
    retry = recheck_targets(cache)
    print(f"failing checks: {len(retry)} rows -> regenerate with the normal API (up to {MAX_ATTEMPTS - 1} calls each)")
    tokens_in = tokens_out = 0
    for i, place_id in enumerate(retry, 1):
        prev = cache[place_id]
        before_in, before_out = prev["input_tokens"], prev["output_tokens"]
        try:
            cache[place_id] = result = generate_checked(client, prev["input_facts"], previous=prev)
            result["input_facts"] = prev["input_facts"]
            tokens_in += result["input_tokens"] - before_in
            tokens_out += result["output_tokens"] - before_out
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            save_json(CACHE_PATH, cache)
            sys.exit(f"stopping: {type(e).__name__}: {e}")
        except (anthropic.APIError, RuntimeError, ValueError, KeyError) as e:
            print(f"{place_id}: retry failed ({type(e).__name__}: {e})")
        if i % SAVE_EVERY == 0:
            save_json(CACHE_PATH, cache)
    save_json(CACHE_PATH, cache)
    flagged = sum(1 for v in cache.values() if v.get("check_issues"))
    print(f"retries: tokens in {tokens_in}, out {tokens_out}, cost ${tokens_in * PRICE_IN + tokens_out * PRICE_OUT:.4f}; "
          f"still flagged after {MAX_ATTEMPTS} attempts: {flagged}")
    cmd_apply(args)


# ------------------------------------------------------------------ cleanup of rows that kept failing (no API call)

SENTENCE_SPLIT = {"en": r"(?<=[.!?])\s+", "zh_tw": r"(?<=[。！？])", "ko": r"(?<=[.!?。])\s+"}
JOIN = {"en": " ", "zh_tw": "", "ko": " "}


def cmd_cleanup(args):
    """Rows still flagged after MAX_ATTEMPTS (decided 2026-11-16):
    - drop sentences with clock times or meal-use words; empty the language only when no sentence is left
    - empty the language when kana remains outside the shop / landmark name
    Other issues stay flagged for review. The original text is kept under "before_cleanup"."""
    cache = load_json(CACHE_PATH, {})
    stats = collections.Counter()
    for pid, v in cache.items():
        if not v.get("check_issues"):
            continue
        facts = v["input_facts"]
        v.setdefault("before_cleanup", {lang: v[lang] for lang in ("en", "zh_tw", "ko")})
        for lang in ("en", "zh_tw", "ko"):
            sents = [s for s in re.split(SENTENCE_SPLIT[lang], v[lang]) if s.strip()]
            keep = [s for s in sents if not (has_time(lang, s) or USE_WORDS[lang].search(s))]
            if len(keep) < len(sents):
                stats[f"{lang}: sentences dropped"] += len(sents) - len(keep)
            text = JOIN[lang].join(s.strip() for s in keep)
            if sents and not keep:
                stats[f"{lang}: emptied (no sentence left)"] += 1
            elif KANA.search(strip_known_names(facts, lang, text)):
                text = ""
                stats[f"{lang}: emptied (kana)"] += 1
            v[lang] = text
        remaining = [i for i in check_description(facts, v)
                     if not any(lang_empty and i.startswith(lang) for lang, lang_empty in
                                ((l, not v[l]) for l in ("en", "zh_tw", "ko")))]
        if remaining:
            v["check_issues"] = remaining
        else:
            v.pop("check_issues", None)
        v["cleaned"] = True
        stats["rows cleaned"] += 1
    save_json(CACHE_PATH, cache)
    for k, n in sorted(stats.items()):
        print(f"  {k}: {n}")
    print(f"still flagged: {sum(1 for v in cache.values() if v.get('check_issues'))}")


def name_forms(v, lang):
    """Ways the shop is named in this language: the name, its parts, and the subject of the
    original first sentence (the model romanizes or translates the name, e.g. 'Wafūtei Naha')."""
    name = v["input_facts"].get("name") or ""
    forms = [x for x in NAME_SEPARATORS.split(name) if len(x) >= 2] + [name]
    first = re.split(SENTENCE_SPLIT[lang], v["before_cleanup"][lang])[0]
    if lang == "en":
        m = re.match(r"(?:Located in [^,]+, |Open [^,]+, |Closed [^,]+, )?(.{2,60}?) (?:is|serves|offers|sits)\b", first)
    else:
        m = re.match(r"(.{2,30}?)(?:是|은|는|이|가)", first)
    if m:
        forms.append(m.group(1))
    return [x for x in forms if x]


def cmd_drop_nameless(args):
    """After cleanup (decided 2026-11-16, option A): a language left with a single sentence that
    does not name the shop ('Its limited schedule makes it ...') is emptied."""
    cache = load_json(CACHE_PATH, {})
    stats = collections.Counter()
    for v in cache.values():
        if not v.get("cleaned"):
            continue
        for lang in ("en", "zh_tw", "ko"):
            text = v[lang]
            if not text or text == v["before_cleanup"][lang]:
                continue
            if len([s for s in re.split(SENTENCE_SPLIT[lang], text) if s.strip()]) != 1:
                continue
            if not any(x in text for x in name_forms(v, lang)):
                v[lang] = ""
                stats[lang] += 1
    save_json(CACHE_PATH, cache)
    print(f"emptied single sentences without the shop name: {dict(stats)}")


def cmd_drop_times(args):
    """All rows (decided 2026-11-16): drop sentences that write out clock times in any form
    (11:00, 오전 9시, 上午9點, 9 a.m.); empty the language only when no sentence is left.
    The single-nameless-sentence rule (drop-nameless) is NOT applied here. Originals: before_time_cleanup."""
    cache = load_json(CACHE_PATH, {})
    stats = collections.Counter()
    rows = 0
    for v in cache.values():
        touched = False
        for lang in ("en", "zh_tw", "ko"):
            text = v.get(lang) or ""
            sents = [x for x in re.split(SENTENCE_SPLIT[lang], text) if x.strip()]
            keep = [x for x in sents if not has_time(lang, x)]
            if len(keep) == len(sents):
                continue
            v.setdefault("before_time_cleanup", {})[lang] = text
            v[lang] = JOIN[lang].join(x.strip() for x in keep)
            stats[f"{lang}: sentences dropped"] += len(sents) - len(keep)
            if not keep:
                stats[f"{lang}: emptied"] += 1
            touched = True
        rows += touched
    for v in cache.values():  # refresh flags with the current checks
        issues = check_description(v["input_facts"], v) if v.get("input_facts") else []
        issues = [i for i in issues if not (not v[i.split(":")[0]] and ("missing" in i))]
        if issues:
            v["check_issues"] = issues
        else:
            v.pop("check_issues", None)
    save_json(CACHE_PATH, cache)
    print(f"rows changed: {rows}; " + ", ".join(f"{k} {n}" for k, n in sorted(stats.items())))
    print(f"still flagged: {sum(1 for v in cache.values() if v.get('check_issues'))}")


def cmd_regen(args):
    """Regenerate the given place_ids from scratch (normal API). The replaced text is kept as previous_version."""
    ids = json.load(open(args.ids_file, encoding="utf-8"))
    if len(ids) > CONFIRM_ABOVE and not args.yes:
        sys.exit(f"{len(ids)} rows: more than {CONFIRM_ABOVE}, re-run with --yes")
    cache = load_json(CACHE_PATH, {})
    client = anthropic.Anthropic()
    t_in = t_out = 0
    for pid in ids:
        old = cache[pid]
        result = generate_checked(client, old["input_facts"])
        t_in += result["input_tokens"]
        t_out += result["output_tokens"]
        result["input_facts"] = old["input_facts"]
        result["previous_version"] = {k: old.get(k) for k in ("en", "zh_tw", "ko", "before_cleanup", "check_issues")}
        cache[pid] = result
        print(f"{old['input_facts']['name']}: attempts {result['attempts']}"
              f"{', still ' + str(result['check_issues']) if result.get('check_issues') else ', ok'}")
    save_json(CACHE_PATH, cache)
    print(f"regen: {len(ids)} rows; tokens in {t_in}, out {t_out}, cost ${t_in * PRICE_IN + t_out * PRICE_OUT:.4f}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("estimate")
    bs = sub.add_parser("batch-submit")
    bs.add_argument("--limit", type=int)
    bs.add_argument("--yes", action="store_true")
    sub.add_parser("batch-status")
    sub.add_parser("batch-collect")
    sub.add_parser("cleanup")
    sub.add_parser("drop-nameless")
    sub.add_parser("drop-times")
    rg = sub.add_parser("regen")
    rg.add_argument("ids_file")
    rg.add_argument("--yes", action="store_true")
    run = sub.add_parser("run")
    run.add_argument("--limit", type=int)
    run.add_argument("--yes", action="store_true")
    show = sub.add_parser("show")
    show.add_argument("--limit", type=int, default=10)
    sub.add_parser("apply")
    args = ap.parse_args()
    {"estimate": cmd_estimate, "run": cmd_run, "show": cmd_show, "apply": cmd_apply,
     "batch-submit": cmd_batch_submit, "batch-status": cmd_batch_status,
     "batch-collect": cmd_batch_collect, "cleanup": cmd_cleanup,
     "drop-nameless": cmd_drop_nameless, "regen": cmd_regen, "drop-times": cmd_drop_times}[args.cmd](args)


if __name__ == "__main__":
    main()
