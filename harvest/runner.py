"""runner.py — harvest one site inside a GitHub Actions job.

    python harvest/runner.py --site galaxynovels --budget-min 330
    python harvest/runner.py --site galaxynovels --all          # this job's share of every site

Why it looks like this: a GitHub job dies after 6 hours and takes its disk with it, so all progress must
live in Google Drive. Each run therefore reads state/<site>-0of1.json from Drive (which novels are done,
where the current one stopped), harvests until the time budget is nearly gone, and pushes the chapters it
wrote plus the updated state back to Drive every few minutes — not only at the end, so a killed job still
keeps its work.

Resume is by file existence inside the pushed folder, so nothing is fetched twice.

Four ways to get a chapter list, because sites disagree:
  * "html" (default)  links on the novel page, paged by chapter_pager
  * "sequential"      /slug_1.html, /slug_2.html ... (fanmtl)
  * "madara"          POST <novel>/ajax/chapters/ (WordPress + Madara themes)
  * "numeric"         chapter-{n}; the page renders only the newest ~120, so enumerate 1..max

fetch_url rebuilds a chapter URL before fetching: novelfire serves a JS stub and bounces the real text
to its sister domain novelphoenix.com.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract import best_paragraphs, best_text, text_in_class  # noqa: E402

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "ar,en;q=0.8",
}
REMOTE = os.environ.get("RCLONE_REMOTE", "gdrive")

# --------------------------------------------------------------------------- #
# Site table. Every entry below was proven against the live site on 2026-11-06.
# --------------------------------------------------------------------------- #
SITES = {
    "galaxynovels": {
        "lang": "ar",
        "base": "https://galaxynovels.com",
        "catalogue": {"type": "listing", "url": "https://galaxynovels.com/novels/?page={page}",
                      "re": r'href="(https://galaxynovels\.com/novel/[^"/]+/)"', "max_pages": 500},
        "chapter_re": r"/chapter-\d+/",
        "chapter_pager": "https://galaxynovels.com/novel/{slug}/?chapter_page={page}",
        # this site lists newest-first
        "chapter_sort": r"/chapter-(\d+)/",
        "text_class": "article",
        "delay": 1.0,
    },
    "novltime": {
        "lang": "ar",
        "base": "https://novltime.com",
        "catalogue": {"type": "json_list", "url": "https://novltime.com/api/v1/novels?page={page}",
                      "items": "data", "slug": "slug", "url_template": "https://novltime.com/ar/novel/{value}",
                      "max_pages": 120},
        "chapter_style": "numeric",
        "chapter_url": "https://novltime.com/ar/novel/{slug}/chapter/chapter-{n}",
        # the chapter page says "الفصل 1 من 50"; that beats anything the novel page renders
        "chapter_total_re": r"الفصل\s*\d+\s*من\s*(\d+)",
        # the class this site uses is on every wrapper, so it drags the header in: paragraphs are clean
        "text_class": "",
        "delay": 0.8,
    },
    "sunovels": {
        "lang": "ar",
        "base": "https://sunovels.com",
        "catalogue": {"type": "listing", "url": "https://sunovels.com/library?page={page}",
                      "re": r'href="(/novel/[^"/?#]+)"', "max_pages": 600},
        "chapter_re": r"/novel/[^/]+/\d+$",
        # page 1 of the list is a separate URL; the novel page itself only shows "first / last" links
        "chapter_start": "https://sunovels.com/novel/{slug}?activeTab=chapters",
        "chapter_pager": "https://sunovels.com/novel/{slug}?activeTab=chapters&page={page}",
        "chapter_sort": r"/novel/[^/]+/(\d+)$",
        # chapter-content is wrapped in hex watermarks on this site; paragraph grouping is clean
        "text_class": "",
        "delay": 0.9,
    },
    "kolnovel": {
        "lang": "ar",
        "base": "https://kolnovel.com",
        "catalogue": {"type": "sitemap_index", "url": "https://kolnovel.com/wp-sitemap.xml",
                      "prefix": "https://kolnovel.com/sitemap", "max_pages": 200},
        "chapter_re": r"/chapter-\d+/?$",
        "text_class": "entry-content",
        "delay": 0.8,
    },
    "wuxiadreams": {
        "lang": "english",
        "base": "https://wuxiadreams.com",
        "catalogue": {"type": "listing", "url": "https://wuxiadreams.com/novels?page={page}",
                      "re": r'href="(/novel/[a-z0-9\-]+)"', "max_pages": 800},
        "chapter_re": r"/novel/[^/]+/chapter-\d+$",
        "chapter_pager": "https://wuxiadreams.com/novel/{slug}?page={page}",
        "chapter_sort": r"/chapter-(\d+)$",
        "delay": 0.7,
    },
    "fanmtl": {
        "lang": "english",
        "base": "https://www.fanmtl.com",
        "catalogue": {"type": "listing", "url": "https://www.fanmtl.com/list/all/all-onclick-{page}.html",
                      "re": r'href="(/novel/[a-z0-9\-]+\.html)"', "max_pages": 2000,
                      "skip_re": r"_\d+\.html$"},
        "chapter_style": "sequential",
        "chapter_pager": "https://www.fanmtl.com/e/extend/fy.php?page={page}&wjm={slug}",
        "text_class": "chapter-content",
        "delay": 0.7,
    },
    "wuxiaworldsite": {
        "lang": "english",
        "base": "https://wuxiaworld.site",
        "catalogue": {"type": "listing", "url": "https://wuxiaworld.site/novel/page/{page}/",
                      "re": r'href="(https://wuxiaworld\.site/novel/[a-z0-9\-]+/)"', "max_pages": 800,
                      "skip_re": r"/novel/(page|feed)/"},
        "chapter_style": "madara",
        "chapter_re": r"/novel/[^/]+/(?:chapter-)?\d+/?$",
        "text_class": "read-container",
        "delay": 0.9,
    },
    "novelfire": {
        "lang": "english",
        "base": "https://novelfire.net",
        "catalogue": {"type": "listing", "url": "https://novelfire.net/genre-all/sort-new/status-all/all-novel?page={page}",
                      "re": r'href="(/book/[a-z0-9\-]+)"', "max_pages": 1200},
        "chapter_re": r"/chapter-\d+$",
        "chapter_pager": "https://novelfire.net/book/{slug}/chapters?page={page}",
        "chapter_sort": r"/chapter-(\d+)$",
        "fetch_url": "https://novelphoenix.com/novel/{slug}/chapter-{n}?noti=1",
        "delay": 0.8,
    },
}

# Sites that still need work, in the order a fresh run should prefer them. kolnovel is finished
# (481 novels, 143k chapters) so it is left out of the rotation and only runs when asked for by name.
ROTATION = ["galaxynovels", "novltime", "sunovels", "wuxiadreams", "fanmtl", "wuxiaworldsite", "novelfire"]

# which slot each matrix job owns. A job takes its named site first, then the rest of its slot.
SLOTS = {"galaxynovels": 0, "fanmtl": 1, "wuxiaworldsite": 2}


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


def post(url: str, tries: int = 3) -> str | None:
    """POST with an empty body. Madara's /ajax/chapters/ endpoint takes no parameters."""
    h = {**UA, "X-Requested-With": "XMLHttpRequest",
         "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=b"", method="POST", headers=h)
            return urllib.request.urlopen(req, timeout=45).read().decode("utf-8", "replace")
        except Exception as e:
            log(f"   post retry {i + 1}/{tries} {type(e).__name__}: {str(e)[:60]}")
            time.sleep(2 + i * 4)
    return None


def get_once(url: str) -> str | None:
    """One attempt, no retries: used for chapter-list pagination, where a 404 means "no more pages"."""
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25).read().decode("utf-8", "replace")
    except Exception:
        return None


def rclone(*args: str) -> str:
    cmd = ["rclone", *args, "--bind", "0.0.0.0",
           "--config", os.path.expanduser("~/.config/rclone/rclone.conf")]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"   rclone {' '.join(args[:2])}: {r.stderr.strip()[:200]}")
    return r.stdout


def state_load(key: str) -> dict:
    out = rclone("cat", f"{REMOTE}:novels/state/{key}.json")
    try:
        return json.loads(out)
    except Exception:
        return {"done_novels": [], "current": None, "lease": None}


def whoami() -> str:
    import socket
    return f"{socket.gethostname()}:{os.getpid()}"


def claim(key: str, workdir: Path, wait_min: int = 0, force: bool = False):
    """Take the site lease, waiting for it if another machine holds it.

    Only one machine may work a site: the flat server and the GitHub runner share this lease in Drive.
    A holder renews it every save; if it dies the lease expires within 20 minutes and the waiting machine
    takes over from exactly the same chapter.
    """
    me = whoami()
    deadline = time.time() + wait_min * 60
    while True:
        state = state_load(key)
        lease = state.get("lease") or {}
        now = time.time()
        free = (not lease.get("owner")) or lease["owner"] == me or lease.get("until", 0) <= now
        if force or free:
            state["lease"] = {"owner": me, "until": now + 20 * 60, "beat": now}
            state_save(key, state, workdir)
            log(f"[{key}] lease acquired by {me}")
            return state
        left = int((lease["until"] - now) / 60)
        if now >= deadline:
            log(f"[{key}] still held by {lease['owner']} ({left} min left) - giving up")
            return None
        log(f"[{key}] standing by: {lease['owner']} holds it for {left} more min")
        time.sleep(180)


def state_save(key: str, state: dict, workdir: Path) -> None:
    if isinstance(state.get("lease"), dict) and state["lease"].get("owner"):
        state["lease"]["until"] = time.time() + 20 * 60
        state["lease"]["beat"] = time.time()
    p = workdir / f"{key}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    rclone("copyto", str(p), f"{REMOTE}:novels/state/{key}.json")


def push(site: str, lang: str, workdir: Path) -> None:
    src = workdir / site
    if src.exists():
        rclone("copy", str(src), f"{REMOTE}:novels/{lang}/{site}",
               "--transfers", "4", "--checkers", "8", "--tpslimit", "8",
               "--fast-list", "--ignore-existing")


def write_site_manifest(site: str, cfg: dict, workdir: Path, novels_known: int) -> None:
    """novels/<lang>/<site>/_site.json — what this folder is and how it was built."""
    src = workdir / site
    if not src.exists():
        return
    folders = [d for d in src.iterdir() if d.is_dir()]
    chapters = sum(sum(1 for _ in d.glob("*.txt")) for d in folders)
    man = {
        "site": site, "language": cfg["lang"], "url": cfg["base"],
        "method": cfg.get("chapter_style", "html"),
        "text_selector": cfg.get("text_class") or "paragraph heuristic",
        "novels": len(folders), "chapters": chapters,
        "catalogue_size": novels_known,
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    p = workdir / f"_site-{site}.json"
    p.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    rclone("copyto", str(p), f"{REMOTE}:novels/{cfg['lang']}/{site}/_site.json")


def slugify(u: str) -> str:
    """A folder name a human can read.

    Arabic novel URLs are percent-encoded on several sites (galaxynovels, kolnovel). Decoding first turns
    %d8%a7%d9%84... into the actual title instead of a folder called -d8-a7-d9-84, and keeps the Drive
    layout readable without a rename pass later.
    """
    tail = urllib.parse.unquote(u.rstrip("/").split("/")[-1])
    tail = re.sub(r"\.html?$", "", tail)
    return re.sub(r"[^\w\-]+", "-", tail).strip("-")[:70] or "novel"


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #
def catalogue(cfg: dict, workdir: Path, site: str, crawl_until: float | None = None) -> list[str]:
    """Load the catalogue from Drive and, if there is time, crawl a few more pages.

    A site like fanmtl lists ~18,000 novels at 30 per page, so building the whole list costs more time than
    one runner has. The crawl saves its page cursor and continues on the next run, while harvesting already
    works from the part that is known. Never shrinks a good catalogue.
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

    def save():
        data.update({"novels": novels, "count": len(novels),
                     "built": time.strftime("%Y-%m-%d %H:%M:%S")})
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        rclone("copyto", str(p), f"{REMOTE}:novels/catalogue/{site}.json")

    if c["type"] == "sitemap":
        if not novels:
            raw = get(c["url"]) or ""
            prefix = c.get("prefix", "")
            for loc in re.findall(r"<loc>([^<]+)</loc>", raw):
                if loc.startswith(prefix) and loc.rstrip("/") != prefix.rstrip("/"):
                    novels.append(loc)
            data["done"] = True
            log(f"   sitemap: {len(novels)} novels")
    elif c["type"] == "sitemap_index":
        if not novels:
            root = get(c["url"]) or ""
            subs = [u for u in re.findall(r"<loc>([^<]+)</loc>", root) if c.get("prefix", "") in u]
            log(f"   sitemap index: {len(subs)} child sitemaps")
            for u in subs:
                raw = get_once(u) or ""
                for loc in re.findall(r"<loc>([^<]+)</loc>", raw):
                    m = re.match(r"(https://[^/]+)/(?:novel|series)/([^/?#]+)/?$", loc)
                    if m:
                        novels.append(f"{m.group(1)}/novel/{m.group(2)}/")
            novels = list(dict.fromkeys(novels))
            data["done"] = True
            log(f"   sitemap index: {len(novels)} novels")
    elif c["type"] == "json_list":
        if not data.get("done"):
            page = int(data.get("next_page") or 1)
            seen = set(novels)
            tpl = c["url_template"]
            while page <= c.get("max_pages", 500):
                if crawl_until and time.time() > crawl_until:
                    log(f"   catalogue: time up at page {page} (keeping {len(novels)})")
                    break
                raw = get(c["url"].format(page=page))
                if not raw:
                    break
                try:
                    items = json.loads(raw).get(c["items"], [])
                except Exception:
                    break
                urls = [tpl.format(value=it.get(c["slug"])) for it in items if it.get(c["slug"])]
                new = [u for u in urls if u not in seen]
                if not new:
                    data["done"] = True
                    log(f"   catalogue finished at page {page} (total {len(novels)})")
                    break
                novels.extend(new)
                seen.update(new)
                page += 1
                data["next_page"] = page
                if page % 10 == 0:
                    log(f"   catalogue page {page}: total {len(novels)}")
                    save()
                time.sleep(0.4)
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
                save()
            time.sleep(0.6)

    save()
    return novels


# --------------------------------------------------------------------------- #
# Chapter list
# --------------------------------------------------------------------------- #
def chapters_for(cfg: dict, novel_url: str, first_page: str) -> list[str]:
    """Return every chapter of a novel, following whatever the site uses to list them.

    Most novel sites show only the first ~30 chapters on the novel page, so a single fetch silently
    truncates the book. That was the bug that made every novel stop at 31 chapters.
    """
    slug = slugify(novel_url)
    style = cfg.get("chapter_style", "html")

    def links_in(html: str) -> list[str]:
        if style == "sequential":
            m = re.match(r"(https://www\.fanmtl\.com/novel/([a-z0-9\-]+))\.html", novel_url)
            if not m:
                return []
            stem = re.escape(m.group(2))
            return [f"{m.group(1)}_{n}.html" for n in
                    sorted({int(x) for x in re.findall(stem + r"_(\d+)\.html", html)})]
        out = []
        for h in dict.fromkeys(re.findall(r'href="([^"]+)"', html)):
            if not re.search(cfg["chapter_re"], h):
                continue
            if h.startswith("/"):
                h = cfg["base"] + h
            if h.startswith("http"):
                out.append(h)
        return out

    # Madara: one POST returns the complete list, newest first.
    if style == "madara":
        raw = post(novel_url.rstrip("/") + "/ajax/chapters/") or ""
        found = links_in(raw)
        found.reverse()
        return sort_chapters(cfg, found)

    # numeric: the page renders only the newest ~120 links, but chapter URLs are chapter-{n}
    if style == "numeric":
        nums = [int(n) for n in re.findall(r"/chapter-(\d+)", first_page)]
        if cfg.get("chapter_total_re"):
            nums += [int(n) for n in re.findall(cfg["chapter_total_re"], first_page)]
        if not nums:
            return []
        return [cfg["chapter_url"].format(slug=slug, n=i) for i in range(1, max(nums) + 1)]

    collected: list[str] = []
    pager = cfg.get("chapter_pager")
    page = 1
    html = first_page
    if cfg.get("chapter_start"):
        html = get(cfg["chapter_start"].format(slug=slug, novel=novel_url.rstrip("/"))) or first_page
    empty_runs = 0
    while True:
        found = links_in(html)
        new = [x for x in found if x not in collected]
        collected.extend(new)
        # one blank page is a flaky response, not the end of the list: two in a row is the real end
        empty_runs = empty_runs + 1 if not new else 0
        if not pager or empty_runs >= 2 or page >= 300:
            break
        page += 1
        nxt = pager.format(page=page, slug=slug, novel=novel_url.rstrip("/"))
        html = get_once(nxt) or ""
        if not html:
            empty_runs += 1
            if empty_runs >= 2:
                break
            continue
        time.sleep(0.4)

    if style == "sequential" and collected:
        m = re.match(r"(https://www\.fanmtl\.com/novel/([a-z0-9\-]+))\.html", novel_url)
        if m:
            nums = [int(re.search(r"_(\d+)\.html$", u).group(1)) for u in collected]
            return [f"{m.group(1)}_{i}.html" for i in range(1, max(nums) + 1)]
    return sort_chapters(cfg, collected)


def sort_chapters(cfg: dict, links: list[str]) -> list[str]:
    """Put chapters in reading order.

    galaxynovels serves its chapter list newest-first, so without this 0001.txt is chapter 3236.
    The number in the URL is the chapter number on every site that does this.
    """
    pat = cfg.get("chapter_sort")
    if not pat:
        return links
    def key(u: str) -> int:
        m = re.search(pat, u)
        return int(m.group(1)) if m else 10 ** 9
    return sorted(links, key=key)


def chapter_links(cfg: dict, novel_url: str, links: list[str]) -> list[str]:
    """Apply fetch_url: rebuild every chapter URL from its position when the site serves stubs."""
    if not cfg.get("fetch_url"):
        return links
    slug = slugify(novel_url)
    return [cfg["fetch_url"].format(slug=slug, n=i + 1) for i in range(len(links))]


def chapter_body(cfg: dict, html: str) -> tuple[str, str]:
    """Story text as one paragraph per line.

    Paragraph grouping beats a CSS class alone: menus, breadcrumbs and Prev/Next rows are built from
    links and list items, so they never enter the paragraph set, and no per-site selector is needed.
    """
    title, _ = best_text(html)
    body = ""
    if cfg.get("text_class"):
        body = text_in_class(html, cfg["text_class"])
    if len(body) < 800:
        body = best_paragraphs(html) or body
    return title, body


# --------------------------------------------------------------------------- #
# Harvest one site
# --------------------------------------------------------------------------- #
def harvest(site: str, cfg: dict, budget_min: int, limit_novels: int = 0, force: bool = False,
            hard_deadline: float | None = None, wait_min: int | None = None) -> None:
    workdir = Path("/tmp/work")
    novel_dir = workdir / site
    novel_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + budget_min * 60
    if hard_deadline:
        deadline = min(deadline, hard_deadline)

    key = f"{site}-0of1"
    state = claim(key, workdir, wait_min=budget_min if wait_min is None else wait_min, force=force)
    if state is None:
        return
    done = set(state.get("done_novels", []))
    crawl_until = time.time() + budget_min * 60 * 0.2
    if hard_deadline:
        crawl_until = min(crawl_until, hard_deadline)
    novels = catalogue(cfg, workdir, site, crawl_until)
    todo = [n for n in novels if n not in done]
    if limit_novels:
        todo = todo[:limit_novels]
    log(f"[{site}] catalogue {len(novels)} - todo {len(todo)} - budget {budget_min} min")
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
        links = chapter_links(cfg, novel_url, chapters_for(cfg, novel_url, page))
        if not links:
            log(f"[{site}] no chapters found: {novel_url[:90]}")
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
                raw = get_once(links[i]) if cfg.get("fetch_url") else get(links[i])
                if not raw:
                    continue
                title, body = chapter_body(cfg, raw)
                paras = [x for x in body.split("\n\n") if x.strip()]
                if len(paras) < 3:
                    continue
                f.write_text((title or f"{slug} {i + 1}") + "\n\n" + "\n\n".join(paras) + "\n",
                             encoding="utf-8", newline="\n")
                manifest[f.name] = {"file": f.name, "chapter": i + 1, "title": (title or "")[:120]}
                written += 1
                time.sleep(cfg.get("delay", 1.0))
            if written and written % 25 == 0 and time.time() - last_push > 240:
                manifest_path.write_text(json.dumps({"novel": slug, "url": novel_url,
                                                     "chapters": sorted(manifest.values(), key=lambda c: c["file"])},
                                                    ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
                push(site, cfg["lang"], workdir)
                state["current"] = {"novel": novel_url, "chapter": i + 2}
                state_save(key, state, workdir)
                last_push = time.time()
                log(f"[{site}] pushed {written} so far (at {slug} ch{i + 1})")

        manifest_path.write_text(json.dumps({"novel": slug, "url": novel_url,
                                             "chapters": sorted(manifest.values(), key=lambda c: c["file"])},
                                            ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        done.add(novel_url)
        state["done_novels"] = sorted(done)
        state["current"] = None
        state_save(key, state, workdir)
        push(site, cfg["lang"], workdir)
        last_push = time.time()
        log(f"[{site}] novel done: {slug} ({len(links)} chapters) - total written {written}")

    push(site, cfg["lang"], workdir)
    write_site_manifest(site, cfg, workdir, len(novels))
    state["lease"] = {"owner": None, "until": 0, "beat": time.time()}
    state_save(key, state, workdir)
    log(f"[{site}] run finished - written {written} - novels done {len(done)}/{len(novels)}")


def run_all(sites: list[str], budget_min: int, force: bool) -> None:
    """Harvest several sites back to back inside one job, sharing the budget.

    The workflow matrix has one job per site. A site must never need a workflow-file edit (that needs a
    token scope this machine does not have), so each job also drains its deterministic slice of the other
    sites: the one it is named for first, then the rest. One owner per site still holds, because every
    site keeps its own lease.
    """
    hard = time.time() + (budget_min - 6) * 60
    for i, site in enumerate(sites):
        left_min = int((hard - time.time()) / 60)
        if left_min < 10:
            log(f"[rotate] out of time, {len(sites) - i} sites left for the next run")
            break
        share = max(10, left_min // (len(sites) - i))
        log(f"[rotate] {site}: {share} min of {left_min} left")
        try:
            harvest(site, SITES[site], share, force=force, hard_deadline=hard, wait_min=3)
        except Exception as e:
            log(f"[rotate] {site} failed: {type(e).__name__}: {e}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True, choices=list(SLOTS))
    ap.add_argument("--budget-min", type=int, default=300)
    ap.add_argument("--limit-novels", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="take the lease even if another machine holds it")
    ap.add_argument("--only", action="store_true",
                    help="harvest just this one site instead of this job's whole rotation slot")
    ap.add_argument("--all", action="store_true", help=argparse.SUPPRESS)  # kept so old commands still run
    args = ap.parse_args()

    if args.only:
        harvest(args.site, SITES[args.site], args.budget_min, limit_novels=args.limit_novels, force=args.force)
        return
    slot = SLOTS[args.site]
    mine = [s for i, s in enumerate(ROTATION) if i % len(SLOTS) == slot]
    rotate = [args.site] + [s for s in mine if s != args.site]
    run_all(rotate, args.budget_min, args.force)


if __name__ == "__main__":
    main()
