"""runner.py — harvest one site inside a GitHub Actions job.

    python harvest/runner.py --site galaxynovels --budget-min 300

Why it looks like this: a GitHub job dies after 6 hours and takes its disk with it, so all progress must
live in Google Drive. Each run therefore:

  1. reads `state/<site>.json` from Drive (which novels are finished, where the current one stopped),
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

# Every site: how to list its novels, how to find chapters, where the text lives.
SITES = {
    "galaxynovels": {
        "lang": "ar", "base": "https://galaxynovels.com",
        "catalogue": {"type": "listing", "url": "https://galaxynovels.com/novels/?page={page}",
                      "re": r'href="(https://galaxynovels\.com/novel/[^"/]+/)"', "max_pages": 120},
        "chapter_re": r"/chapter-\d+/",
        "text_class": "article",
        "delay": 1.2,
    },
    "fanmtl": {
        "lang": "en", "base": "https://www.fanmtl.com",
        "catalogue": {"type": "listing", "url": "https://www.fanmtl.com/list/all/all-onclick-{page}.html",
                      "re": r'href="(/novel/[a-z0-9\-]+\.html)"', "max_pages": 300},
        "skip_re": r"_\d+\.html$",
        "chapter_style": "sequential",   # /novel/<slug>_<n>.html
        "text_class": "chapter-content",
        "delay": 0.8,
    },
    "wuxiaworldsite": {
        "lang": "en", "base": "https://wuxiaworld.site",
        "catalogue": {"type": "sitemap", "url": "https://wuxiaworld.site/sitemap.xml",
                      "prefix": "https://wuxiaworld.site/novel/"},
        "chapter_re": r"/chapter-?\d+/?$",
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


def rclone(*args: str, check: bool = False) -> str:
    cmd = ["rclone", *args, "--config", os.path.expanduser("~/.config/rclone/rclone.conf")]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        log(f"rclone {' '.join(args[:2])} failed: {r.stderr[:200]}")
    return r.stdout


def state_load(site: str) -> dict:
    out = rclone("cat", f"{REMOTE}:novels/state/{site}.json")
    try:
        return json.loads(out)
    except Exception:
        return {"done_novels": [], "current": None}


def state_save(site: str, state: dict, workdir: Path) -> None:
    p = workdir / f"{site}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    rclone("copyto", str(p), f"{REMOTE}:novels/state/{site}.json")


def push(site: str, lang: str, workdir: Path) -> None:
    src = workdir / site
    if not src.exists():
        return
    rclone("copy", str(src), f"{REMOTE}:novels/{lang}/{site}", "--transfers", "8", "--checkers", "16",
           "--fast-list", "--ignore-existing")


def slugify(u: str) -> str:
    tail = u.rstrip("/").split("/")[-1]
    tail = re.sub(r"\.html?$", "", tail)
    return re.sub(r"[^A-Za-z0-9_\-\u0600-\u06FF]+", "-", tail)[:70] or "novel"


def catalogue(cfg: dict, workdir: Path, site: str) -> list[str]:
    """Prefer a catalogue already in Drive; build it only when missing."""
    p = workdir / f"catalogue-{site}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    rclone("copyto", f"{REMOTE}:novels/catalogue/{site}.json", str(p))
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("novels"):
                log(f"[{site}] catalogue from Drive: {len(data['novels'])} novels")
                return data["novels"]
        except Exception:
            pass
    c = cfg["catalogue"]
    novels = []
    if c["type"] == "listing":
        page = 1
        while page <= c.get("max_pages", 100):
            raw = get(c["url"].format(page=page))
            if not raw:
                break
            found = re.findall(c["re"], raw)
            if c.get("skip_re"):
                found = [f for f in found if not re.search(c["skip_re"], f)]
            found = [f if f.startswith("http") else cfg["base"] + f for f in found]
            new = [f for f in dict.fromkeys(found) if f not in novels]
            novels.extend(new)
            log(f"   catalogue page {page}: +{len(new)} (total {len(novels)})")
            if not new:
                break
            page += 1
            time.sleep(1.0)
    else:
        if c["type"] == "sitemap":
            raw = get(c["url"]) or ""
            prefix = c.get("prefix", "")
            for loc in re.findall(r"<loc>([^<]+)</loc>", raw):
                if loc.startswith(prefix) and loc.rstrip("/") != prefix.rstrip("/"):
                    novels.append(loc)
            log(f"   sitemap: {len(novels)} novels")
    p.write_text(json.dumps({"site": site, "built": time.strftime("%Y-%m-%d %H:%M:%S"), "novels": novels},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    rclone("copyto", str(p), f"{REMOTE}:novels/catalogue/{site}.json")
    return novels


def chapters_for(cfg: dict, novel_url: str, page: str) -> list[str]:
    if cfg.get("chapter_style") == "sequential":
        m = re.match(r"(https://www\.fanmtl\.com/novel/[a-z0-9\-]+)\.html", novel_url)
        if not m:
            return []
        stem = m.group(1)
        nums = [int(n) for n in re.findall(re.escape(stem) + r"_(\d+)\.html", page)]
        if not nums:
            return []
        return [f"{stem}_{i}.html" for i in range(1, max(nums) + 1)]
    out = []
    for h in dict.fromkeys(re.findall(r'href="([^"]+)"', page)):
        if not re.search(cfg["chapter_re"], h):
            continue
        if h.startswith("/"):
            h = cfg["base"] + h
        if h.startswith("http"):
            out.append(h)
    return out


def harvest(site: str, cfg: dict, budget_min: int) -> None:
    workdir = Path("/tmp/work")
    novel_dir = workdir / site
    novel_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + budget_min * 60

    state = state_load(site)
    done = set(state.get("done_novels", []))
    novels = catalogue(cfg, workdir, site)
    todo = [n for n in novels if n not in done]
    log(f"[{site}] {len(novels)} novels · {len(todo)} left · budget {budget_min} min")

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
                state_save(site, state, workdir)
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
                state_save(site, state, workdir)
                last_push = time.time()
                log(f"[{site}] pushed {written} so far (at {slug} ch{i + 1})")

        manifest_path.write_text(json.dumps({"novel": slug, "url": novel_url,
                                             "chapters": sorted(manifest.values(), key=lambda c: c["file"])},
                                            ensure_ascii=False, indent=1), encoding="utf-8")
        done.add(novel_url)
        state["done_novels"] = sorted(done)
        state["current"] = None
        state_save(site, state, workdir)
        push(site, cfg["lang"], workdir)
        last_push = time.time()
        log(f"[{site}] novel done: {slug} ({len(links)} chapters) · total written {written}")

    push(site, cfg["lang"], workdir)
    log(f"[{site}] run finished · written {written} · novels done {len(done)}/{len(novels)}")


ap = argparse.ArgumentParser()
ap.add_argument("--site", required=True, choices=list(SITES))
ap.add_argument("--budget-min", type=int, default=300)
args = ap.parse_args()
harvest(args.site, SITES[args.site], args.budget_min)
