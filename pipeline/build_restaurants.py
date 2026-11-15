"""
Google Sheets (restaurant list) -> restaurants.geojson for MOP-SSG.

  python build_restaurants.py fetch                 # download the sheet as CSV
  python build_restaurants.py geocode [--limit N]   # Nominatim, 1 req/s, resumable cache
  python build_restaurants.py build   [--limit N]   # CSV + cache -> data/restaurants.geojson
  python build_restaurants.py upload                # put data/restaurants.geojson to S3

All state lives in ./data (git-ignored):
  restaurants.csv       downloaded sheet
  geocode_cache.json    place_id -> Nominatim result (or failure); makes geocode resumable
  pin_ids.json          place_id -> pin_id; keeps pin_id / URL stable when the sheet is re-sorted
  restaurants.geojson   build output
"""

import argparse
import collections
import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

import pykakasi

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CSV_PATH = os.path.join(DATA, "restaurants.csv")
CACHE_PATH = os.path.join(DATA, "geocode_cache.json")
PIN_PATH = os.path.join(DATA, "pin_ids.json")
OUT_PATH = os.path.join(DATA, "restaurants.geojson")

def load_env(path):
    """Minimal KEY=VALUE reader for pipeline/.env (git-ignored); real env vars win."""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())


load_env(os.path.join(HERE, ".env"))

# Private values live in .env (see .env.example), not in the repo
SHEET_CSV_URL = os.environ.get("SHEET_CSV_URL", "")
S3_BUCKET = "s3geojsonnew"
S3_KEY = "restaurants.geojson"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim policy asks for an identifying User-Agent with contact info
USER_AGENT = os.environ.get("NOMINATIM_USER_AGENT", "MOPApp/1.0")
MIN_INTERVAL = 1.05  # seconds between Nominatim requests (policy: max 1 req/s)
# Results outside Okinawa Prefecture are treated as wrong matches
OKINAWA_BBOX = (24.0, 27.95, 122.9, 131.4)  # lat_min, lat_max, lng_min, lng_max


# --------------------------------------------------------------------------- io

def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj, compact=False):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)  # atomic: an interrupted run never leaves a half-written cache


def read_rows(limit=None):
    with open(CSV_PATH, encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("place_id", "").strip()]
    return rows[:limit] if limit else rows


# ------------------------------------------------------------------- geocoding

def normalize_address(addr):
    a = unicodedata.normalize("NFKC", addr)          # full-width digits/hyphens -> ASCII
    a = re.sub(r"〒?\s*\d{3}-\d{4}", " ", a)          # postal code
    a = re.sub(r"[‐‑‒–—―ー−]", "-", a)               # dash variants (after digits)
    a = re.sub(r"^日本[、,]\s*", "", a)
    return re.sub(r"\s+", " ", a).strip()


def address_queries(addr):
    """Full address first, then the address with the trailing house number / building removed."""
    full = normalize_address(addr)
    queries = [full]
    # e.g. 沖縄県うるま市勝連平敷屋430-2 -> 沖縄県うるま市勝連平敷屋
    trimmed = re.sub(r"[\s\d\-]+[^\d]*$", "", full).strip() if re.search(r"\d", full) else ""
    if trimmed and trimmed != full and len(trimmed) >= 5:
        queries.append(trimmed)
    return queries


_last_request = 0.0


def nominatim(query):
    """Returns the first result dict, None for no result. Raises on network/HTTP errors."""
    global _last_request
    wait = MIN_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    params = urllib.parse.urlencode({
        "q": query, "format": "jsonv2", "limit": 1, "countrycodes": "jp", "accept-language": "ja",
    })
    req = urllib.request.Request(f"{NOMINATIM_URL}?{params}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            results = json.load(res)
    finally:
        _last_request = time.monotonic()
    return results[0] if results else None


def in_okinawa(lat, lng):
    return OKINAWA_BBOX[0] <= lat <= OKINAWA_BBOX[1] and OKINAWA_BBOX[2] <= lng <= OKINAWA_BBOX[3]


QUERY_CACHE_PATH = os.path.join(DATA, "query_cache.json")
_query_cache = None  # query string -> Nominatim result (or None); many shops share a town-level query


def cached_nominatim(q):
    global _query_cache
    if _query_cache is None:
        _query_cache = load_json(QUERY_CACHE_PATH, {})
    if q in _query_cache:
        return _query_cache[q]
    for attempt in range(3):
        try:
            r = nominatim(q)
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(30 * (attempt + 1))  # back off politely
                continue
            raise
    _query_cache[q] = r
    return r


def geocode_row(addr):
    """-> cache entry. Network errors propagate so the row is retried on the next run."""
    for level, q in enumerate(address_queries(addr)):
        r = cached_nominatim(q)
        if r:
            lat, lng = float(r["lat"]), float(r["lon"])
            if in_okinawa(lat, lng):
                return {"ok": True, "lat": lat, "lng": lng, "type": r.get("type"), "query": q, "level": level}
    return {"ok": False}


def cmd_geocode(args):
    rows = read_rows(args.limit)
    cache = load_json(CACHE_PATH, {})
    todo = [r for r in rows if r["place_id"] not in cache]
    total, done0 = len(rows), len(rows) - len(todo)
    print(f"geocode: {total} rows, {done0} cached, {len(todo)} to go (~{len(todo) * 1.5 / 3600:.1f} h)", flush=True)
    errors = 0
    for i, r in enumerate(todo, 1):
        try:
            cache[r["place_id"]] = geocode_row(r["住所"])
        except Exception as e:  # network trouble: leave uncached, retry on next run
            errors += 1
            print(f"  ! {r['place_id']} {type(e).__name__}: {e}", flush=True)
            if errors >= 20:
                print("too many consecutive errors, stopping (re-run to resume)", flush=True)
                break
            time.sleep(10)
            continue
        errors = 0
        if i % 10 == 0 or i == len(todo):
            save_json(CACHE_PATH, cache)
            save_json(QUERY_CACHE_PATH, _query_cache or {})
        if i % 100 == 0 or i == len(todo):
            ok = sum(1 for p in (x["place_id"] for x in rows) if cache.get(p, {}).get("ok"))
            print(f"{done0 + i}/{total} done ({ok} with coordinates)", flush=True)
    save_json(CACHE_PATH, cache)
    save_json(QUERY_CACHE_PATH, _query_cache or {})


# ------------------------------------------------------------------ transforms

DAYS = {"月曜日": "Mo", "火曜日": "Tu", "水曜日": "We", "木曜日": "Th", "金曜日": "Fr", "土曜日": "Sa", "日曜日": "Su"}
DAY_ORDER = list(DAYS.values())
TIME_RE = re.compile(r"(\d{1,2})時(\d{2})分")


def parse_day_value(v):
    """'11時00分～14時00分, 17時00分～22時00分' -> '11:00-14:00,17:00-22:00'; '定休日' -> 'off'; else None."""
    v = unicodedata.normalize("NFKC", v).strip()
    if v == "定休日":
        return "off"
    if re.fullmatch(r"24\s*時間営業", v):
        return "00:00-24:00"
    ranges = []
    for part in re.split(r"\s*,\s*", v):
        m = re.fullmatch(r"(\d{1,2})時(\d{2})分\s*[~〜]\s*(?:翌)?(\d{1,2})時(\d{2})分", part)
        if not m:
            return None
        h1, m1, h2, m2 = m.groups()
        ranges.append(f"{int(h1):02d}:{m1}-{int(h2):02d}:{m2}")
    return ",".join(ranges) if ranges else None


def to_osm_hours(src):
    """Google '月曜日: 11時00分～13時00分 | 火曜日: 定休日 | ...' -> ('Mo 11:00-13:00; Tu-Su off', None).
    Returns (original, None) when any day cannot be parsed."""
    if not src.strip():
        return None, None
    per_day = {}
    for seg in src.split("|"):
        if ":" not in seg:
            return src.strip(), None
        day, val = seg.split(":", 1)
        code = DAYS.get(day.strip())
        parsed = parse_day_value(val) if code else None
        if parsed is None:
            return src.strip(), None
        per_day[code] = parsed
    if set(per_day) != set(DAY_ORDER):
        return src.strip(), None

    # group consecutive days with identical hours: Mo-Fr 11:00-19:00; Sa,Su off
    groups = []
    for d in DAY_ORDER:
        if groups and groups[-1][1] == per_day[d] and DAY_ORDER.index(groups[-1][0][-1]) == DAY_ORDER.index(d) - 1:
            groups[-1][0].append(d)
        else:
            groups.append(([d], per_day[d]))
    by_value = {}
    for days, val in groups:
        label = days[0] if len(days) == 1 else f"{days[0]}-{days[-1]}"
        by_value.setdefault(val, []).append(label)
    rules = [f"{','.join(labels)} {val}" for val, labels in by_value.items() if val != "off"]
    if "off" in by_value:
        rules.append(f"{','.join(by_value['off'])} off")
    return "; ".join(rules), None


PRICE = {
    "¥": ("〜¥1,000", 1),
    "¥¥": ("¥1,000〜¥2,000", 2),
    "¥¥¥": ("¥2,000〜¥3,000", 3),
    "¥¥¥¥": ("¥3,000〜", 4),
}

_kks = pykakasi.kakasi()


def romanize(name):
    text = unicodedata.normalize("NFKC", name)
    roman = " ".join(item["hepburn"] for item in _kks.convert(text))
    roman = re.sub(r"[^a-z0-9]+", "-", roman.lower()).strip("-")
    return roman[:60].rstrip("-")


def num(v, cast):
    try:
        return cast(v) if str(v).strip() not in ("", "-") else None
    except ValueError:
        return None


# 2026-11-16 decision: places whose primary genre is not a place to eat or drink are left out.
# Only the primary (first) genre counts, so e.g. "noodle_shop / hotel" stays in.
NON_FOOD_GENRES = {
    # lodging
    "hotel", "resort_hotel", "extended_stay_hotel", "budget_japanese_inn", "inn", "guest_house",
    "bed_and_breakfast", "hostel", "lodging", "cottage", "campground",
    # sights, leisure, venues
    "tourist_attraction", "amusement_park", "amusement_center", "indoor_playground", "park",
    "botanical_garden", "museum", "art_museum", "art_gallery", "performing_arts_theater",
    "golf_course", "sports_complex", "fishing_charter", "tour_agency", "ranch", "farm",
    "wedding_venue", "banquet_hall", "barbecue_area", "karaoke", "internet_cafe",
    # shops that are not eateries
    "shopping_mall", "clothing_store", "book_store", "furniture_store", "sporting_goods_store",
    "florist", "gift_shop", "pet_store", "liquor_store", "wholesaler",
    # services, offices
    "hair_salon", "nail_salon", "spa", "yoga_studio", "medical_clinic", "health",
    "corporate_office", "coworking_space", "non_profit_organization", "manufacturer",
    "parking_lot",
}


def primary_genre(row):
    return row["業態分類"].split("/")[0].strip()


def assign_pin_ids(rows):
    """place_id -> pin_id, stable across runs; new places get the next free number."""
    pins = load_json(PIN_PATH, {})
    used = [int(p[3:]) for p in pins.values()]
    nxt = max(used, default=0) + 1
    for r in rows:
        if r["place_id"] not in pins:
            pins[r["place_id"]] = f"okr{nxt:05d}"
            nxt += 1
    save_json(PIN_PATH, pins)
    return pins


# Fields edited by hand after the first build (descriptions, translations, publish flags).
# A rebuild keeps their previous values instead of resetting them from the sheet.
EDITORIAL_FIELDS = [
    "approved", "show_on_map", "title_zh_tw", "title_ko", "address_zh-tw", "address_ko", "photo_url",
    "description_ja", "description_en", "description_zh_tw", "description_ko",
    "opening_hours_note_en", "opening_hours_note_zh_tw",
]


def previous_editorial():
    prev = load_json(OUT_PATH, {"features": []})
    return {
        f["properties"]["place_id"]: {k: f["properties"][k] for k in EDITORIAL_FIELDS if k in f["properties"]}
        for f in prev.get("features", []) if f.get("properties", {}).get("place_id")
    }


def cmd_build(args):
    rows = read_rows(args.limit)
    cache = load_json(CACHE_PATH, {})
    keep = previous_editorial()
    pins = assign_pin_ids(read_rows())  # always register the whole sheet so numbering follows sheet order
    today = dt.date.today().isoformat()

    features, unconverted_hours, missing_geo = [], 0, 0
    excluded = collections.Counter()
    for r in rows:
        if primary_genre(r) in NON_FOOD_GENRES:
            excluded[primary_genre(r)] += 1
            continue
        pid = r["place_id"]
        pin = pins[pid]
        name = r["店舗名"].strip()
        geo = cache.get(pid)
        has_geo = bool(geo and geo.get("ok"))
        missing_geo += not has_geo
        hours, hours_note = to_osm_hours(r["営業時間"])
        if hours and not re.match(r"^[A-Z][a-z]", hours):
            unconverted_hours += 1
        price_range, price_level = PRICE.get(r["価格帯"].strip(), (None, None))
        roman = romanize(name)
        address = r["住所"].strip()

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [geo["lng"], geo["lat"]]} if has_geo else None,
            "properties": {
                "pin_id": pin,
                "id": pin,
                "slug": f"{pin}-{roman}" if roman else pin,
                "title": name,
                "title_ja": name,
                "title_zh_tw": None,
                "title_ko": None,
                "address_ja": address,
                "address": address,
                "address_zh-tw": None,
                "address_ko": None,
                "phone_number": r["電話番号"].strip() or None,
                "opening_hours": hours,
                "opening_hours_note_ja": hours_note,
                "price_range": price_range,
                "price_level": price_level,
                "category": "4",
                "genre": primary_genre(r) or None,
                "geocode_source": "nominatim" if has_geo else None,
                "geocode_precision": geo.get("type") if has_geo else None,
                # 2026-11-15 decision: coordinates are town-level, so no map pins yet (geometry is kept)
                "show_on_map": False,
                # 2026-11-15 decision: unpublished until a description is written; flip to "1" per record
                "approved": "0",
                "official_url": r["ウェブサイトURL"].strip() or None,
                "description_ja": None,
                "description_en": None,
                "description_zh_tw": None,
                "description_ko": None,
                "photo_url": None,
                "place_id": pid,
                "google_maps_url": r["GoogleマップURL"].strip() or None,
                "rating": num(r["評価点数"], float),
                "review_count": num(r["レビュー件数"], int),
                "source": "google_places",
                "updated_at": today,
            },
        })
        features[-1]["properties"].update(keep.get(pid, {}))

    save_json(OUT_PATH, {"type": "FeatureCollection", "features": features}, compact=True)
    n = len(features)
    print(f"build: {n} features -> {OUT_PATH} ({os.path.getsize(OUT_PATH) / 1e6:.1f} MB)")
    print(f"  with coordinates: {n - missing_geo} ({(n - missing_geo) / n:.1%}), without: {missing_geo}")
    print(f"  opening_hours kept as original text: {unconverted_hours}")
    print(f"  excluded as non-food genre: {sum(excluded.values())} {dict(excluded.most_common())}")


def cmd_fetch(args):
    if not SHEET_CSV_URL:
        sys.exit("fetch: set SHEET_CSV_URL in pipeline/.env")
    os.makedirs(DATA, exist_ok=True)
    req = urllib.request.Request(SHEET_CSV_URL, headers={"User-Agent": "MOPApp/1.0"})
    with urllib.request.urlopen(req, timeout=60) as res, open(CSV_PATH + ".tmp", "wb") as f:
        f.write(res.read())
    os.replace(CSV_PATH + ".tmp", CSV_PATH)
    print(f"fetch: {len(read_rows())} rows -> {CSV_PATH}")


def cmd_upload(args):
    subprocess.run([
        "aws", "s3api", "put-object", "--bucket", S3_BUCKET, "--key", S3_KEY, "--body", OUT_PATH,
        "--content-type", "application/json; charset=utf-8", "--server-side-encryption", "AES256",
        "--query", "VersionId", "--output", "text",
    ], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["fetch", "geocode", "build", "upload"])
    ap.add_argument("--limit", type=int, help="only the first N rows (for testing)")
    args = ap.parse_args()
    {"fetch": cmd_fetch, "geocode": cmd_geocode, "build": cmd_build, "upload": cmd_upload}[args.command](args)


if __name__ == "__main__":
    main()
