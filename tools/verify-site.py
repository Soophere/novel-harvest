"""verify-site.py — prove a site config on ONE real novel before harvesting wide.

    python tools/verify-site.py --site wuxiadreams --url https://wuxiadreams.com/novel/shadow-slave
    python tools/verify-site.py --site novelfire --url https://novelfire.net/book/infinite-bloodcore --fetch 5

It runs the exact functions the runner runs (catalogue -> chapters_for -> chapter_body) but writes nothing
to Drive and takes no lease, so it is safe to run while a harvest job is live.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "harvest"))

import runner  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True, choices=list(runner.SITES))
    ap.add_argument("--url", default="", help="novel page URL (default: first entry of the catalogue)")
    ap.add_argument("--fetch", type=int, default=3, help="how many chapters to actually download")
    ap.add_argument("--catalogue", type=int, default=0, help="also prove the catalogue (pages to crawl)")
    a = ap.parse_args()
    cfg = runner.SITES[a.site]

    if a.catalogue:
        t0 = time.time()
        novels = runner.catalogue(cfg, Path("/tmp/verify"), a.site, time.time() + a.catalogue * 20)
        print(f"catalogue: {len(novels)} novels in {time.time() - t0:.0f}s")
        for u in novels[:3]:
            print("   ", u)
        if not novels:
            return 1
        url = a.url or novels[0]
    else:
        if not a.url:
            print("pass --url or --catalogue")
            return 2
        url = a.url

    t0 = time.time()
    page = runner.get(url)
    if not page:
        print(f"FAIL novel page {url}")
        return 1
    links = runner.chapter_links(cfg, url, runner.chapters_for(cfg, url, page))
    print("   order:", links[0].rsplit("/", 1)[-1][:40], "->", links[-1].rsplit("/", 1)[-1][:40])
    print(f"chapters: {len(links)} in {time.time() - t0:.1f}s")
    if not links:
        return 1
    print("   first:", links[0])
    print("   last :", links[-1])

    ok = 0
    for i in range(min(a.fetch, len(links))):
        t = time.time()
        body_title, body = runner.chapter_body(cfg, runner.get(links[i]) or "")
        paras = [p for p in body.split("\n\n") if p.strip()]
        print(f"  ch{i + 1:04d}: {len(body):6d} chars, {len(paras):3d} paras, {time.time() - t:.1f}s")
        print("      ", (paras[0] if paras else "")[:120])
        if len(body) > 1500 and len(paras) >= 5:
            ok += 1
    print(f"RESULT: {ok}/{min(a.fetch, len(links))} chapters look like story text")
    return 0 if ok == min(a.fetch, len(links)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
