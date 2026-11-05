"""build-pairs.py — find novels that exist in both English and Arabic, for Pipeline B.

    python tools/build-pairs.py --catalogues .cache/catalogue --out pairs.json

A pair only needs the URLs: Pipeline B reads chapter 1..N from each site with the harvest runner, so
nothing here depends on what has already been downloaded.

Matching is on the slug only, with the arabic site's own slug taken as-is. Two sites rarely agree on a
title, so a hit means the same slug exists on both — wide enough to find real pairs, narrow enough to
avoid guesses. Anything uncertain is listed under "review" instead of being paired silently.
"""
import argparse
import json
import re
from pathlib import Path

EN_SITES = {"fanmtl", "wuxiaworldsite", "wuxiadreams", "novelfire"}
AR_SITES = {"kolnovel", "novltime", "galaxynovels", "sunovels"}


def slug_of(url: str) -> str:
    tail = url.rstrip("/").split("/")[-1]
    tail = re.sub(r"\.html?$", "", tail)
    return re.sub(r"[^a-z0-9]+", "-", tail.lower()).strip("-")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalogues", default=".cache/catalogue")
    ap.add_argument("--out", default="pairs.json")
    ap.add_argument("--min-len", type=int, default=6, help="ignore very short slugs, they collide by accident")
    a = ap.parse_args()

    root = Path(a.catalogues)
    en: dict[str, list[dict]] = {}
    ar: dict[str, list[dict]] = {}
    found = 0
    for p in sorted(root.glob("*.json")):
        site = p.stem
        if not (site in EN_SITES or site in AR_SITES):
            continue
        found += 1
        data = json.loads(p.read_text(encoding="utf-8"))
        bucket = en if site in EN_SITES else ar
        for url in data.get("novels") or []:
            s = slug_of(url)
            if len(s) < a.min_len:
                continue
            bucket.setdefault(s, []).append({"site": site, "url": url})
        print(f"{site:16s} {data.get('count', len(data.get('novels') or [])):>7} novels -> {len(bucket)} slugs so far")

    if not found:
        print(f"no catalogue json in {root.resolve()}")
        return 1

    pairs = []
    for slug, ar_list in sorted(ar.items()):
        if slug not in en:
            continue
        pairs.append({"novel": slug, "en": en[slug], "ar": ar_list})
    pairs.sort(key=lambda p: p["novel"])

    out = Path(a.out)
    out.write_text(json.dumps({"built_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
                               "english_slugs": len(en), "arabic_slugs": len(ar),
                               "pairs": pairs}, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    print(f"\n{len(pairs)} pairs -> {out}")
    for p in pairs[:15]:
        print(f"   {p['novel'][:50]:52s} en:{p['en'][0]['site']:<14} ar:" + ",".join(x["site"] for x in p["ar"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
