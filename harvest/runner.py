"""runner.py — harvest one site inside a GitHub Actions job.

    python harvest/runner.py --site galaxynovels --budget-min 330

Why it looks like this: a GitHub job dies after 6 hours and takes its disk with it, so all progress must
live in Google Drive. Each run therefore:

  1. reads `state/<site>-<shard>.json` from Drive (which novels are finished, where the current one stopped),
  2. harvests until the time budget is nearly gone,
  3. pushes the chapters it wrote plus the updated state back to Drive — every few minutes, not only at the end,
     so a killed job still keeps its work.

Resume is by file existence inside the pushed folder, so nothing is fetched twice.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract import best_text, text_in_class  # noqa: E402

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "ar,en;q=0.8",
}
REMOTE = os.environ.get("RCLONE_REMOTE", "gdrive")

# Every site: how to list its novels, how to walk its chapter list, where the text lives.
SITES = {
    "galaxynovels": {
        "lang": "ar", "base": "https://galaxynovels.com",
        "catalogue": {"type": "listing", "url": "https://galaxynovels.com/novels/?page={page}",
                      "re": r'href="(https://galaxynovels\.com/novel/[^"/]+/)"', "max_pages": 500},
        "chapter_re": r"/chapter-\d+/",
        # the novel page shows ~30 chapters and paginates the rest
        "chapter_pager": "https://galaxynovels.com/novel/{slug}/?chapter_page={page}",
        "text_class": "article",
        "delay": 1.0,
    },
    "fanmtl": {
        "lang": "en", "base": "https://www.fanmtl.com",
        "catalogue": {"type": "listing", "url": "https://www.fanmtl.com/list/all/all-onclick-{page}.html",
                      "re": r'href="(/novel/[a-z0-9\-]+\.html)"', "max_pages": 2000,
                      "skip_re": r"_\d+\.html$"},
        "chapter_style": "sequential",   # chapters are /novel/<slug>_<n>.html
        "chapter_pager": "https://www.fanmtl.com/e/extend/fy.php?page={page}&wjm={slug}",
        "text_class": "chapter-content",
        "delay": 0.7,
    },
    "wuxiaworldsite": {
        "lang": "en", "base": "https://wuxiaworld.site",
        "catalogue": {"type": "sitemap", "url": "https://wuxiaworld.site/sitemap.xml",
                      "prefix": "https://wuxiaworld.site/novel/"},
        "chapter_re": r"/chapter-?\d+/?$",
        "chapter_pager": "https://wuxiaworld.site/novel/{slug}/page/{page}/",
        "text_class": "entry-content",
        "delay": 1.0,
    },
}


def log(*a):
    print(*a, flush=True)


def get(url: str, tries: int = 5) -> str | None:
    for i in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40).read().decode("utf-8", "replace")
        except Exception as e:
            log(f"   retry {i + 1}/{tries} {type(e).__name__}: {str(e)[:60]}")
            time.sleep(2 + i * 4)
    return None


def rclone(*args: str) -> str:
    cmd = ["rclone", *args, "--config", os.path.expanduser("~/.config/rclone/rclone.conf")]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"   rclone {' '.join(args[:2])}: {r.stderr.strip()[:200]}")
    return r.stdout


def state_load(key: str) -> dict:
    out = rclone("cat", f"{REMOTE}:novels/state/{key}.json")
    try:
        return json.loads(out)
    except Exception:
        return {"done_novels": [], "current": None}


def state_save(key: str, state: dict, workdir: Path) -> None:
    p = workdir / f"{key}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    rclone("copyto", str(p), f"{REMOTE}:novels/state/{key}.json")


def push(site: str, lang: str, workdir: Path) -> None:
    src = workdir / site
    if src.exists():
        rclone("copy", str(src), f"{REMOTE}:novels/{lang}/{site}", "--transfers", "8", "--checkers", "16",
               "--fast-list", "--ignore-existing")


def slugify(u: str) -> str:
    tail = u.rstrip("/").split("/")[-1]
    tail = re.sub(r"\.html?$", "", tail)
    return re.sub(r"[^A-Za-z0-9_\-\u0600-\u06FF]+", "-", tail)[:70] or "novel"


def catalogue(cfg: dict, workdir: Path, site: str, crawl_until: float | None = None) -> list[str]:
    """Load the catalogue from Drive and, if there is time, crawl a few more pages.

    A site like fanmtl lists ~18,000 novels at 30 per page, so building the whole list costs more time than
    one runner has. The crawl saves its page cursor and continues on the next run, while harvesting already
    works from the part that is known.
    """
    p = workdir / f"catalogue-{site}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    rclone("copyto", f"{REMOTE}:novels/catalogue/{site}.json", str(p))
    data = {"site": site, "built": None, "novels": [], "done": False, "next_page": 1}
    if p.exists():
        try:
            data.update(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    novels = data.get("novels") or []
    if novels:
        log(f"[{site}] catalogue: {len(novels)} known, next page {data.get('next_page')}, done={data.get('done')}")

    c = cfg["catalogue"]
    if c["type"] == "sitemap":
        if not novels:
            raw = get(c["url"]) or ""
            prefix = c.get("prefix", "")
            for loc in re.findall(r"<loc>([^<]+)</loc>", raw):
                if loc.startswith(prefix) and loc.rstrip("/") != prefix.rstrip("/"):
                    novels.append(loc)
            data["done"] = True
            log(f"   sitemap: {len(novels)} novels")
    elif not data.get("done"):
        page = int(data.get("next_page") or 1)
        seen = set(novels)
        while page <= c.get("max_pages", 1000):
            if crawl_until and time.time() > crawl_until:
                log(f"   catalogue: time up at page {page} (keeping {len(novels)})")
                break
            raw = get(c["url"].format(page=page))
            if not raw:
                break
            found = re.findall(c["re"], raw)
            if c.get("skip_re"):
                found = [f for f in found if not re.search(c["skip_re"], f)]
            found = [f if f.startswith("http") else cfg["base"] + f for f in found]
            new = [f for f in dict.fromkeys(found) if f not in seen]
            if not new:
                data["done"] = True
                log(f"   catalogue finished at page {page} (total {len(novels)})")
                break
            novels.extend(new)
            seen.update(new)
            page += 1
            data["next_page"] = page
            if page % 25 == 0:
                log(f"   catalogue page {page}: total {len(novels)}")
                data["novels"] = novels
                p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                rclone("copyto", str(p), f"{REMOTE}:novels/catalogue/{site}.json")
            time.sleep(0.6)

    data.update({"novels": novels, "built": time.strftime("%Y-%m-%d %H:%M:%S")})
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    rclone("copyto", str(p), f"{REMOTE}:novels/catalogue/{site}.json")
    return novels


def chapters_for(cfg: dict, novel_url: str, first_page: str) -> list[str]:
    """Return every chapter of a novel, following the site's chapter-list pagination.

    Most novel sites show only the first ~30 chapters on the novel page, so a single fetch silently
    truncates the book. `chapter_pager` says how to ask for the following pages.
    """
    slug = slugify(novel_url)

    def links_in(html: str) -> list[str]:
        if cfg.get("chapter_style") == "sequential":
            m = re.match(r"(https://www\.fanmtl\.com/novel/[a-z0-9\-]+)\.html", novel_url)
            if not m:
                return []
            stem = re.escape(m.group(1))
            return [f"{m.group(1)}_{n}.html" for n in sorted({int(x) for x in re.findall(stem + r"_(\d+)\.html", html)})]
        out = []
        for h in dict.fromkeys(re.findall(r'href="([^"]+)"', html)):
            if not re.search(cfg["chapter_re"], h):
                continue
            if h.startswith("/"):
                h = cfg["base"] + h
            if h.startswith("http"):
                out.append(h)
        return out

    collected: list[str] = []
    pager = cfg.get("chapter_pager")
    page = 1
    html = first_page
    while True:
        found = links_in(html)
        new = [x for x in found if x not in collected]
        collected.extend(new)
        if not pager or not new or page >= 300:
            break
        page += 1
        html = get(pager.format(page=page, slug=slug, novel=novel_url.rstrip("/")))
        if not html:
            break
        time.sleep(0.5)

    if cfg.get("chapter_style") == "sequential" and collected:
        m = re.match(r"(https://www\.fanmtl\.com/novel/[a-z0-9\-]+)\.html", novel_url)
        if m:
            nums = [int(re.search(r"_(\d+)\.html$", u).group(1)) for u in collected]
            return [f"{m.group(1)}_{i}.html" for i in range(1, max(nums) + 1)]
    return collected


def harvest(site: str, cfg: dict, budget_min: int, worker: int = 0, of: int = 1, limit_novels: int = 0) -> None:
    workdir = Path("/tmp/work")
    novel_dir = workdir / site
    novel_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + budget_min * 60

    key = f"{site}-{worker}of{of}"
    state = state_load(key)
    done = set(state.get("done_novels", []))
    crawl_until = time.time() + budget_min * 60 * 0.2
    novels = catalogue(cfg, workdir, site, crawl_until)
    if of > 1:
        novels = novels[worker::of]
    todo = [n for n in novels if n not in done]
    if limit_novels:
        todo = todo[:limit_novels]
    log(f"[{site}] catalogue {len(novels)} · todo {len(todo)} · budget {budget_min} min")
    written = 0
    last_push = time.time()

    for novel_url in todo:
        if time.time() > deadline:
            log(f"[{site}] budget reached")
            break
        cur = state.get("current") or {}
        start_from = cur.get("chapter", 1) if cur.get("novel") == novel_url else 1
        page = get(novel_url)
        if not page:
            continue
        links = chapters_for(cfg, novel_url, page)
        if not links:
            done.add(novel_url)
            state["done_novels"] = sorted(done)
            state["current"] = None
            state_save(key, state, workdir)
            continue
        slug = slugify(novel_url)
        folder = novel_dir / slug
        folder.mkdir(parents=True, exist_ok=True)
        manifest_path = folder / "index.json"
        manifest = {}
        if manifest_path.exists():
            try:
                manifest = {c["file"]: c for c in json.loads(manifest_path.read_text(encoding="utf-8")).get("chapters", [])}
            except Exception:
                manifest = {}

        for i in range(start_from - 1, len(links)):
            if time.time() > deadline:
                state["current"] = {"novel": novel_url, "chapter": i + 1}
                state_save(key, state, workdir)
                push(site, cfg["lang"], workdir)
                log(f"[{site}] budget reached mid-novel {slug} at chapter {i + 1}")
                return
            f = folder / f"{i + 1:04d}.txt"
            if not f.exists():
                raw = get(links[i])
                if not raw:
                    continue
                body = text_in_class(raw, cfg["text_class"]) if cfg.get("text_class") else ""
                title, fallback = best_text(raw)
                if len(body) < 300:
                    body = fallback
                paras = [x for x in body.split("\n\n") if x.strip()]
                if len(paras) < 3:
                    continue
                f.write_text((title or f"{slug} {i + 1}") + "\n\n" + "\n\n".join(paras) + "\n", encoding="utf-8", newline="\n")
                manifest[f.name] = {"file": f.name, "chapter": i + 1, "title": (title or "")[:120]}
                written += 1
                time.sleep(cfg.get("delay", 1.0))
            if written and written % 25 == 0 and time.time() - last_push > 240:
                manifest_path.write_text(json.dumps({"novel": slug, "url": novel_url,
                                                     "chapters": sorted(manifest.values(), key=lambda c: c["file"])},
                                                    ensure_ascii=False, indent=1), encoding="utf-8")
                push(site, cfg["lang"], workdir)
                state["current"] = {"novel": novel_url, "chapter": i + 2}
                state_save(key, state, workdir)
                last_push = time.time()
                log(f"[{site}] pushed {written} so far (at {slug} ch{i + 1})")

        manifest_path.write_text(json.dumps({"novel": slug, "url": novel_url,
                                             "chapters": sorted(manifest.values(), key=lambda c: c["file"])},
                                            ensure_ascii=False, indent=1), encoding="utf-8")
        done.add(novel_url)
        state["done_novels"] = sorted(done)
        state["current"] = None
        state_save(key, state, workdir)
        push(site, cfg["lang"], workdir)
        last_push = time.time()
        log(f"[{site}] novel done: {slug} ({len(links)} chapters) · total written {written}")

    push(site, cfg["lang"], workdir)
    log(f"[{site}] run finished · written {written} · novels done {len(done)}/{len(novels)}")


ap = argparse.ArgumentParser()
ap.add_argument("--site", required=True, choices=list(SITES))
ap.add_argument("--budget-min", type=int, default=300)
ap.add_argument("--worker", type=int, default=0)
ap.add_argument("--of", type=int, default=1)
ap.add_argument("--limit-novels", type=int, default=0)
args = ap.parse_args()
harvest(args.site, SITES[args.site], args.budget_min, args.worker, args.of, args.limit_novels)
