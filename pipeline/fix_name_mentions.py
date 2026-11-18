"""
Make the English descriptions name each restaurant the way name_en does (2026-11-18).

The descriptions were written from the Japanese name before name_en existed, so the model romanized
names on its own ("Stake House Tokushige", "Pokeman Ginowan") or left them in Japanese script. This
script, with no API calls:

  1. applies hand-checked name corrections from data/name_fixes.json (en / zh / ko; place-name readings,
     review decisions, Cyrillic look-alikes)
  2. applies hand-written description edits (data/name_fixes.json "desc": [[old, new], ...]) to the original
     text: names mixing scripts, leftover model reasoning. Those shops skip step 3.
  3. replaces each misspelt name in description_en with name_en. The misspelt spans come from
     data/name_mention_spans.json (found by fuzzy matching, then filtered: generic words, lower-case
     spans, shortened names and style-only differences such as Honten / Main Branch are left alone)
  4. replaces a Japanese-script name (title_ja) in description_en with name_en, including the forms
     "romaji (日本語)" and "日本語 (romaji)", and other Japanese readings in brackets. A sentence about
     the Japanese name itself is kept.
  5. replaces Japanese municipality names in description_en ("南城市" -> "Nanjo City")
  6. writes the same values into names_cache.json / descriptions_cache.json so a later `apply` keeps them

Usage:
  python fix_name_mentions.py            dry run: counts, samples, leftovers; writes nothing
  python fix_name_mentions.py --write    backs up restaurants.geojson, then writes it and both caches
"""
import argparse
import difflib
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
GEO = os.path.join(DATA, "restaurants.geojson")
FIXES = os.path.join(DATA, "name_fixes.json")  # shop names: kept out of the public repo, like data/
SPANS = os.path.join(DATA, "name_mention_spans.json")
NAMES_CACHE = os.path.join(DATA, "names_cache.json")
DESC_CACHE = os.path.join(DATA, "descriptions_cache.json")
MUNI_TS = os.path.join(HERE, "..", "src", "lib", "municipalities.ts")

JP = re.compile(r"[぀-ヺー-ヿ㐀-鿿ｦ-ﾟ]")  # not the middle dot ・ (B・B・R)
CYRILLIC = re.compile(r"[Ѐ-ӿ]")


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def municipalities():
    """'南城市' -> 'Nanjo City' (suffix as the descriptions write it: City / Town / Village)."""
    src = open(MUNI_TS, encoding="utf-8").read()
    out = {}
    for ja, en in re.findall(r"(\S+?): L\('([^']+)'", src):
        suffix = {"市": " City", "町": " Town", "村": " Village"}[ja[-1]]
        out[ja] = en if en.endswith(suffix.strip()) else en + suffix
    return dict(sorted(out.items(), key=lambda kv: -len(kv[0])))  # 北中城村 before 中城村


def span_pattern(span):
    words = re.findall(r"[A-Za-z0-9À-ɏ']+", span)
    edge = "A-Za-zÀ-ɏ぀-ヿ㐀-鿿"  # never cut into a word or a Japanese run (MaMa|りん)
    return re.compile(r"(?<![" + edge + "])" + r"[^A-Za-z0-9'぀-ヿ㐀-鿿]+".join(re.escape(w) for w in words)
                      + r"(?:[a-z]*[À-ɏ][A-Za-zÀ-ɏ]*)?"  # the rest of "Shokud|ō" (spans were cut at accents)
                      + r"(?:-(?:ten|branch))?(?![" + edge + "])", re.I)


def replace_span(desc, pattern, en):
    """Replace every match with name_en. A span can stop short of the name the text uses ("Yaeshoku" of
    "Yaeshoku Shokudo", or before a closing bracket), so words right after it that name_en already
    contains are taken in too, and so is a ')' that closes a '(' inside the match."""
    en_words = [w.lower() for w in re.findall(r"[\wÀ-ɏ']+", en)]
    out, pos, n = [], 0, 0
    for m in pattern.finditer(desc):
        end = m.end()
        if m.group(0).count("(") > m.group(0).count(")") and desc[end:end + 1] == ")":
            end += 1
        for _ in range(2):
            nxt = re.match(r"\s+([\wÀ-ɏ']+)", desc[end:])
            if not nxt or not en_words:
                break
            w = nxt.group(1).lower()
            if w in en_words[-2:] or (len(w) >= 3 and w in en_words[-1] and w != en_words[-1]):
                end += nxt.end()
            else:
                break
        begin = m.start()
        # ...and before it: "Sake to [Sakana Chanbaru]" for name_en "Sake and Fish Chanbaru"
        back = re.search(r"((?:[\wÀ-ɏ'&]+\s+){1,3})$", desc[pos:begin])
        if back and en_words:
            words = back.group(1).split()
            for k in range(len(words)):
                ws = [w.lower() for w in words[k:]]
                if ws[0] in en_words[:2] and all(w in en_words or w in ("to", "no", "and", "&", "x") for w in ws):
                    begin = pos + back.start(1) + len(" ".join(words[:k])) + (1 if k else 0)
                    break
        out.append(desc[pos:begin] + en)
        pos, n = end, n + 1
    return "".join(out) + desc[pos:], n


def similarity(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def replace_japanese_name(desc, ja, en, stats):
    if not ja or not JP.search(ja) or ja not in desc:
        return desc
    i = desc.index(ja)
    if re.search(r"Japanese name\W{0,3}$", desc[:i], re.I):
        stats["ja_kept_japanese_name_sentence"] += 1
        return desc
    q = re.escape(ja)
    # 日本語 (romaji) -> name_en
    m = re.search(q + r"\s*[(（][^)）]*[)）]", desc)
    if m:
        stats["ja_then_reading"] += 1
        return desc[:m.start()] + en + desc[m.end():]
    # romaji (日本語) -> name_en; the romaji is the run of words just before the bracket that best matches name_en
    m = re.search(r"([^.,;:!?()（）]*?)\s*[(（]\s*" + q + r"\s*[)）]", desc)
    if m:
        words = m.group(1).split(" ")
        best = max(range(len(words)), key=lambda k: similarity(" ".join(words[k:]), en))
        if similarity(" ".join(words[best:]), en) >= 0.5:
            stats["reading_then_ja"] += 1
            start = m.start(1) + len(" ".join(words[:best])) + (1 if best else 0)
            return desc[:start] + en + desc[m.end():]
        # no romaji close to name_en ("Kurīm Kurīm (クリームクリーム)"): the model's own reading, often wrong.
        # The name starts the clause, so the clause start up to the bracket becomes name_en.
        stats["reading_then_ja_clause"] += 1
        start = m.start(1) + len(m.group(1)) - len(m.group(1).lstrip())
        return desc[:start] + en + desc[m.end():]
    stats["ja_plain"] += 1
    return desc.replace(ja, en)


def japanese_brackets(desc, en, stats):
    """'Kaaraya (瓦家)': a reading in brackets after the name. Put name_en in place of the name when the
    words before the bracket match it, otherwise just drop the bracket."""
    out, pos = [], 0
    for m in re.finditer(r"\s*[(（][^()（）]*[぀-ヿ㐀-鿿][^()（）]*[)）]", desc):
        head = desc[pos:m.start()]
        stops = [x.end() for x in re.finditer(r"[.,;:!?](?=\s)", head)]  # not the dot of ".5 BAR"
        cut = stops[-1] if stops else 0
        words = head[cut:].split(" ")
        k = max(range(len(words)), key=lambda j: similarity(" ".join(words[j:]), en))
        if words[k:] and similarity(" ".join(words[k:]), en) >= 0.5:
            head = head[:cut] + " ".join(words[:k]) + (" " if k else "") + en
            stats["bracket_name_replaced"] += 1
        else:
            stats["bracket_dropped"] += 1
        out.append(head)
        pos = m.end()
    return "".join(out) + desc[pos:]


def tidy(desc, en):
    # "Kappo Hama Kappo Hama" / "Kappo Hama (Kappo Hama)" left over when the text already had the name
    q = re.escape(en)
    desc = re.sub(q + r"\s*[(（]\s*" + q + r"\s*[)）]", en, desc)
    desc = re.sub(q + r"\s+" + q, en, desc)
    desc = re.sub(r"\)(?=[A-Za-z])", ") ", desc)  # "GULGUL (Guru Guru)is"
    return re.sub(r"  +", " ", desc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    geo, fixes, spans = load(GEO), load(FIXES), load(SPANS)
    fixes.pop("_note", None)
    manual = {k: v.pop("desc") for k, v in fixes.items() if "desc" in v}
    muni = municipalities()
    from collections import Counter
    stats, changed, samples = Counter(), {}, []

    for f in geo["features"]:
        p = f["properties"]
        if str(p.get("approved")) != "1":
            continue
        pid = p["id"]
        old_en = p.get("name_en") or ""
        if pid in fixes:
            for lang, val in fixes[pid].items():
                key = "name_" + lang
                if p.get(key) != val:
                    p[key] = val
                    stats["name_" + lang + "_fixed"] += 1
        en = p.get("name_en") or ""
        before = desc = p.get("description_en") or ""
        if not desc:
            continue
        for old, new in manual.get(pid, []):  # hand-written edits for mixed-script names (on the original text, before the automatic steps)
            if old in desc:
                desc = desc.replace(old, new)
                stats["manual_edit"] += 1
            else:
                stats["manual_edit_missing"] += 1
        if pid in spans and pid not in manual and not JP.search(spans[pid]):
            desc, n = replace_span(desc, span_pattern(spans[pid]), en)
            stats["span_replaced" if n else "span_not_found"] += 1
        if old_en and old_en != en and old_en in desc:
            desc = desc.replace(old_en, en)
            stats["old_name_en_replaced"] += 1
        desc = replace_japanese_name(desc, p.get("title_ja") or "", en, stats)
        desc = japanese_brackets(desc, en, stats)
        for ja, label in muni.items():
            if ja in desc:
                desc = desc.replace(ja, label)
                stats["municipality_replaced"] += 1
        desc = tidy(desc, en)
        if desc != before:
            p["description_en"] = desc
            changed[pid] = (p["place_id"], before, desc)
            if len(samples) < 400:
                samples.append((pid, before, desc))

    approved = [f["properties"] for f in geo["features"] if str(f["properties"].get("approved")) == "1"]
    left_jp = [p["id"] for p in approved if JP.search(p.get("description_en") or "")]
    left_cy = [(p["id"], k) for p in approved for k in ("name_en", "name_zh", "name_ko", "description_en")
               if CYRILLIC.search(p.get(k) or "")]
    print(dict(stats))
    print(f"descriptions changed: {len(changed)}")
    print(f"description_en still with Japanese script: {len(left_jp)}")
    print(f"Cyrillic left: {left_cy}")
    report = os.path.join(DATA, "fix_name_mentions.report.json")
    with open(report, "w", encoding="utf-8") as fo:
        json.dump({"stats": stats, "changed": {k: {"before": v[1], "after": v[2]} for k, v in changed.items()},
                   "left_japanese": {i: next(p["description_en"] for p in approved if p["id"] == i) for i in left_jp},
                   "left_cyrillic": left_cy}, fo, ensure_ascii=False, indent=1)
    print("report:", report)

    if not args.write:
        print("dry run: nothing written (use --write)")
        return
    backup = GEO.replace(".geojson", ".before-namefix.geojson")
    shutil.copy2(GEO, backup)
    with open(GEO, "w", encoding="utf-8") as fo:
        json.dump(geo, fo, ensure_ascii=False, separators=(",", ":"))
    names_cache, desc_cache = load(NAMES_CACHE), load(DESC_CACHE)
    by_id = {f["properties"]["id"]: f["properties"] for f in geo["features"]}
    n_names = n_desc = 0
    for pid in fixes:
        p = by_id[pid]
        c = names_cache.get(p["place_id"])
        if c:
            c["en"], c["zh"], c["ko"] = p["name_en"], p["name_zh"], p["name_ko"]
            n_names += 1
    for pid, (place_id, _, after) in changed.items():
        if place_id in desc_cache:
            desc_cache[place_id]["en"] = after
            n_desc += 1
    for path, obj in ((NAMES_CACHE, names_cache), (DESC_CACHE, desc_cache)):
        shutil.copy2(path, path.replace(".json", ".before-namefix.json"))
        with open(path, "w", encoding="utf-8") as fo:
            json.dump(obj, fo, ensure_ascii=False, indent=1)
    print(f"written: {GEO} (backup {os.path.basename(backup)}), names_cache {n_names}, descriptions_cache {n_desc}")


if __name__ == "__main__":
    main()
