"""
Small field fixes to the S3 spot GeoJSON (Geojson3.23.geojson), with the same safety steps as
translate_spots_ko.py (fetch -> patch -> verify only the listed fields changed -> upload with ETag
check and backup).

  python patch_spots.py build          # fetch from S3, apply PATCHES, verify, write data/spots_ko/Geojson3.23.geojson
  python translate_spots_ko.py upload --yes   # back up the S3 object and upload the built file
"""

import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I does not add the script dir
from translate_spots_ko import BUILD_PATH, cmd_fetch, dumps_like_s3, load_original

CJ_EXPEDIA = "https://www.dpbolvw.net/click-101309942-10581071?url="  # CJ PID / Expedia advertiser used on every hotel link
KLOOK = "https://affiliate.klook.com/redirect?aid=100611&aff_adid={adid}&k_site="  # MOP's Klook affiliate id


def cj(url):
    return CJ_EXPEDIA + urllib.parse.quote(url, safe="")


def klook(url, adid):
    return KLOOK.format(adid=adid) + urllib.parse.quote(url, safe="")


# 2026-11-16: zh-TW links that earned nothing (direct Expedia links, Klook link with another aid=79886)
PATCHES = {
    "oki30": {"affiliate_link_tw": cj("https://www.expedia.com.tw/Okinawa-Hotels-Hotel-Gracery-Naha.h12385623.Hotel-Information")},
    # h5963317 = former name Royal Hotel Okinawa Zanpamisaki, same property as the English link
    "oki68": {"affiliate_link_tw": cj("https://www.expedia.com.tw/Okinawa-Hotels-Royal-Hotel-OKINAWA-ZANPAMISAKI.h5963317.Hotel-Information")},
    # aff_adid 1144006 is the ad id of the same activity's English link
    "oki120": {"affiliate_link_tw": klook("https://www.klook.com/zh-TW/activity/116750-padi-open-water-course-in-okinawa-with-padi-resort/", 1144006)},
}


def main():
    if sys.argv[1:] != ["build"]:
        sys.exit(__doc__)
    cmd_fetch(None)
    original = load_original()
    data = json.loads(json.dumps(original))
    for f in data["features"]:
        for k, v in PATCHES.get(f["properties"]["pin_id"], {}).items():
            assert k in f["properties"], (f["properties"]["pin_id"], k)
            f["properties"][k] = v
    changed = []
    for new, old in zip(data["features"], original["features"]):
        assert new["geometry"] == old["geometry"] and new["properties"].keys() == old["properties"].keys()
        for k, v in new["properties"].items():
            if v != old["properties"][k]:
                pin = old["properties"]["pin_id"]
                assert k in PATCHES.get(pin, {}), (pin, k)
                changed.append((pin, k, old["properties"][k][:70], v))
    assert len(changed) == sum(len(p) for p in PATCHES.values()), changed
    with open(BUILD_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(dumps_like_s3(data))
    for pin, k, old, new in changed:
        print(f"{pin}.{k}\n  old: {old}...\n  new: {new}")
    print(f"verified: only {len(changed)} fields changed -> {BUILD_PATH}")


if __name__ == "__main__":
    main()
