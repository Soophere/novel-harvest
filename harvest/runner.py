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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract import best_paragraphs, best_text, text_in_class  # noqa: E402

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "ar,en;q=0.8",
}
REMOTE = os.environ.get("RCLONE_REMOTE", "gdrive")

# How many novels of each site to take, from the top of its catalogue. 0 = the whole site.
# fanmtl and wuxiaworld.site each hold ~9-11 thousand novels averaging 2,500 chapters: harvesting them
# whole is 39 million chapters and about seven months of continuous work for material nobody reads.
# 300 each is roughly half a million chapters and about two weeks.
TOP_N = int(os.environ.get("HARVEST_TOP_N", "300"))

# --------------------------------------------------------------------------- #
# Site table. Every entry below was proven against the live site on 2026-11-06.
# --------------------------------------------------------------------------- #
SITES = {
    "galaxynovels": {
        "lang": "arabic",
        "base": "https://galaxynovels.com",
        "catalogue": {"type": "listing", "url": "https://galaxynovels.com/novels/?page={page}",
                      "re": r'href="(https://galaxynovels\.com/novel/[^"/]+/)"', "max_pages": 500},
        "chapter_re": r"/chapter-\d+/",
        "chapter_pager": "https://galaxynovels.com/novel/{slug}/?chapter_page={page}",
        # this site lists newest-first
        "chapter_sort": r"/chapter-(\d+)/",
        "text_class": "article",
        "rate": 6,
    },
    "novltime": {
        "lang": "arabic",
        "base": "https://novltime.com",
        "catalogue": {"type": "json_list", "url": "https://novltime.com/api/v1/novels?page={page}",
                      "items": "data", "slug": "slug", "url_template": "https://novltime.com/ar/novel/{value}",
                      # page=2 is a 400; this API only paginates by cursor
                      "cursor_url": "https://novltime.com/api/v1/novels?cursor={cursor}",
                      "cursor_field": "nextCursor", "max_pages": 200},
        "chapter_style": "numeric",
        "chapter_url": "https://novltime.com/ar/novel/{slug}/chapter/chapter-{n}",
        # the chapter page says "الفصل 1 من 50"; that beats anything the novel page renders
        "chapter_total_re": r"الفصل\s*\d+\s*من\s*(\d+)",
        # the class this site uses is on every wrapper, so it drags the header in: paragraphs are clean
        "text_class": "",
        "rate": 8,
    },
    "sunovels": {
        "lang": "arabic",
        "base": "https://sunovels.com",
        "catalogue": {"type": "listing", "url": "https://sunovels.com/library?page={page}",
                      "re": r'href="(/novel/[^"/?#]+)"', "max_pages": 600},
        "chapter_re": r"/novel/[^/]+/\d+$",
        # page 1 of the list is a separate URL; the novel page itself only shows "first / last" links
        "chapter_start": "https://sunovels.com/novel/{slug}?activeTab=chapters",
        "chapter_pager": "https://sunovels.com/novel/{slug}?activeTab=chapters&page={page}",
        "chapter_sort": r"/novel/[^/]+/(\d+)$",
        "catalogue_bad_re": r"/\d+$",
        # chapter-content is wrapped in hex watermarks on this site; paragraph grouping is clean
        "text_class": "",
        "rate": 5,
    },
    "kolnovel": {
        "lang": "arabic",
        "base": "https://kolnovel.com",
        "catalogue": {"type": "sitemap_index", "url": "https://kolnovel.com/wp-sitemap.xml",
                      "prefix": "https://kolnovel.com/sitemap", "max_pages": 200},
        "chapter_re": r"/chapter-\d+/?$",
        "text_class": "entry-content",
        "rate": 8,
    },
    "wuxiadreams": {
        "lang": "english",
        "base": "https://wuxiadreams.com",
        "catalogue": {"type": "listing", "url": "https://wuxiadreams.com/novels?page={page}",
                      "re": r'href="(/novel/[a-z0-9\-]+)"', "max_pages": 800},
        "chapter_re": r"/novel/[^/]+/chapter-\d+$",
        "chapter_pager": "https://wuxiadreams.com/novel/{slug}?page={page}",
        "chapter_sort": r"/chapter-(\d+)$",
        "rate": 6,
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
        "rate": 6,
    },
    "wuxiaworldsite": {
        "lang": "english",
        "base": "https://wuxiaworld.site",
        "catalogue": {"type": "listing", "url": "https://wuxiaworld.site/novel/page/{page}/",
                      "re": r'href="(https://wuxiaworld\.site/novel/[a-z0-9\-]+/)"', "max_pages": 800,
                      "skip_re": r"/novel/(page|feed)/"},
        "chapter_style": "madara",
        "chapter_re": r"/novel/[^/]+/(?:chapter-)?\d+/?$",
        # a chapter URL here ends in a bare number; an older sitemap crawl put 9,981 of them in the catalogue
        "catalogue_bad_re": r"/\d+/?$",
        "text_class": "read-container",
        "rate": 5,
    },
    "novelfire": {
        "lang": "english",
        "base": "https://novelfire.net",
        "catalogue": {"type": "listing", "url": "https://novelfire.net/genre-all/sort-popular/status-all/all-novel?page={page}",
                      "re": r'href="(/book/[a-z0-9\-]+)"', "max_pages": 1200},
        "chapter_re": r"/chapter-\d+$",
        "chapter_pager": "https://novelfire.net/book/{slug}/chapters?page={page}",
        "chapter_sort": r"/chapter-(\d+)$",
        "fetch_url": "https://novelphoenix.com/novel/{slug}/chapter-{n}?noti=1",
        "rate": 8,
    },
}

# Sites that still need work, in the order a fresh run should prefer them. kolnovel is finished
# (481 novels, 143k chapters) so it is left out of the rotation and only runs when asked for by name.
ROTATION = ["galaxynovels", "novltime", "sunovels", "wuxiadreams", "fanmtl", "wuxiaworldsite", "novelfire"]

# The GitHub matrix has one job per site and each job also drains a slice of the rest, because the workflow
# file cannot be edited without the "workflow" token scope. The flat server calls --only --site <name>.
SLOTS = {"galaxynovels": 0, "fanmtl": 1, "wuxiaworldsite": 2}


def log(*a):
    print(*a, flush=True)


class Rate:
    """One request slot every 1/rate seconds, shared by every thread working this site.

    Politeness has to be a property of the *site*, not of each thread: eight threads each sleeping a
    second would hit the server eight times a second. A worker waits for the next slot and then fires.
    """

    def __init__(self, per_second: float):
        self.gap = 1.0 / max(0.1, per_second)
        self.next = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.time()
            slot = max(now, self.next)
            self.next = slot + self.gap
            delay = slot - now
        if delay > 0:
            time.sleep(delay)


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
    """One attempt, except for throttling.

    A 404 means "no more pages" and must not be retried. A 429 means "slow down", and dropping that chapter
    silently is how a novel ends up half-written, so it is retried with a real backoff.
    """
    for attempt in range(3):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25).read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < 2:
                time.sleep(6 * (attempt + 1))
                continue
            return None
        except Exception:
            return None
    return None


LAST_RC = [0]


def rclone(*args: str, timeout: int = 300, quiet: bool = False) -> str:
    """Run rclone with a hard wall-clock limit.

    The flat server's line drops for minutes at a time. Without a limit, rclone's own retries hang the
    whole runner: the loop sat in 'rclone cat state/...' for six minutes doing nothing. A killed rclone
    returns "" and the caller carries on.
    """
    cmd = ["rclone", *args, "--bind", "0.0.0.0",
           "--timeout", "60s", "--contimeout", "20s",
           "--config", os.path.expanduser("~/.config/rclone/rclone.conf")]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"   rclone {' '.join(args[:2])}: timed out after {timeout}s")
        LAST_RC[0] = 1
        return ""
    LAST_RC[0] = r.returncode
    if r.returncode != 0 and not quiet:
        log(f"   rclone {' '.join(args[:2])}: {r.stderr.strip()[:200]}")
    return r.stdout


STATE_DIR = "_state"
CATALOGUE_DIR = "_catalogue"


def migrate_legacy_file(old: str, new: str, ) -> None:
    """Move a machine file from the old flat name to the underscore one, once.

    state/ and catalogue/ used to sit beside the chapter folders. Everything the machines own now starts
    with an underscore, so a listing sorts the machinery to the top and the corpus below it.
    """
    if rclone("lsf", new, quiet=True).strip():
        return
    if not rclone("lsf", old, quiet=True).strip():
        return
    rclone("copyto", old, new)
    log(f"   moved {old.split('novels/')[-1]} -> {new.split('novels/')[-1]}")


def state_load(key: str) -> dict | None:
    """None means Drive is unreachable, which is different from "no state yet".

    A machine that cannot reach Drive must not harvest: it cannot save progress, cannot push chapters and
    cannot hold the lease. The flat server spent 15 minutes per site on this before standing down.
    """
    name = f"{STATE_DIR}/{key}.json"
    migrate_legacy_file(f"{REMOTE}:novels/state/{key}.json", f"{REMOTE}:novels/{name}")
    for attempt in range(2):
        out = rclone("cat", f"{REMOTE}:novels/{name}", timeout=90)
        try:
            return json.loads(out)
        except Exception:
            if not out.strip():
                # rclone reported nothing; either the file is absent or the call failed
                time.sleep(3)
                continue
            time.sleep(3)
    # one last look: is Drive there at all?
    if not rclone("lsd", f"{REMOTE}:novels/", timeout=90).strip():
        log(f"   Drive unreachable - standing down on {key}")
        return None
    log(f"   no state for {key} yet - starting fresh")
    return {"done_novels": [], "current": None, "lease": None}


def whoami() -> str:
    import socket
    return f"{socket.gethostname()}:{os.getpid()}"


def lease_is_dead(lease: dict, me: str) -> bool:
    """True when the lease belongs to a process on THIS host that no longer exists.

    The lease is a host:pid string. Without this, restarting the service leaves a 20-minute lease behind
    that the new process dutifully waits out - the flat server stood still for 16 minutes after one restart.
    Only the same host is checked; a lease from another machine is always honoured until it expires.
    """
    owner = lease.get("owner") or ""
    if not owner or owner == me:
        return False
    host, _, pid = owner.rpartition(":")
    my_host = me.rpartition(":")[0]
    if host != my_host or not pid.isdigit():
        return False
    try:
        os.kill(int(pid), 0)          # signal 0 = "does this process exist?"
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False


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
        if state is None:
            return None
        lease = state.get("lease") or {}
        now = time.time()
        free = ((not lease.get("owner")) or lease["owner"] == me or lease.get("until", 0) <= now
                or lease_is_dead(lease, me))
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
    rclone("copyto", str(p), f"{REMOTE}:novels/{STATE_DIR}/{key}.json")


def push(site: str, lang: str, workdir: Path) -> bool:
    src = workdir / site
    if not src.exists():
        return True
    rclone("copy", str(src), f"{REMOTE}:novels/{lang}/{site}",
           "--transfers", "4", "--checkers", "8", "--tpslimit", "8",
           "--fast-list", "--ignore-existing", timeout=1800)
    if LAST_RC[0] != 0:
        log(f"   push to {lang}/{site} did not complete - chapters are on local disk only")
        return False
    return True


def migrate_legacy_lang(site: str, lang: str) -> None:
    """Move a site out of the old novels/ar/ tree.

    The lang field was "ar" while the documented tree is arabic/, so three Arabic sites harvested correctly
    into a folder nothing reads. The home server cannot do the copy — its uplink stalls on Drive — but the
    runner can, so it heals the tree on every run until there is nothing left to move.
    """
    if lang == "ar":
        return
    legacy = f"{REMOTE}:novels/ar/{site}"
    if not rclone("lsf", legacy, "--dirs-only", "--max-depth", "1", quiet=True).strip():
        return
    log(f"[{site}] migrating legacy novels/ar/{site} -> novels/{lang}/{site}")
    rclone("copy", legacy, f"{REMOTE}:novels/{lang}/{site}",
           "--transfers", "4", "--checkers", "8", "--tpslimit", "8")
    src = len(rclone("lsf", legacy, "-R", "--files-only").splitlines())
    dst = len(rclone("lsf", f"{REMOTE}:novels/{lang}/{site}", "-R", "--files-only").splitlines())
    if dst >= src:
        rclone("purge", legacy)
        log(f"[{site}] migrated {src} files, legacy folder removed")
    else:
        log(f"[{site}] migration incomplete ({dst}/{src}) - keeping both, will retry next run")


def read_json_remote(name: str) -> dict:
    out = rclone("cat", f"{REMOTE}:novels/{name}")
    try:
        return json.loads(out)
    except Exception:
        return {}


def write_json_remote(name: str, data: dict) -> None:
    import tempfile
    tmp = Path(tempfile.gettempdir()) / name.replace("/", "-")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    rclone("copyto", str(tmp), f"{REMOTE}:novels/{name}")


def update_root_index(site: str, cfg: dict, novels_known: int, chapters: int, novels_done: int,
                      status: str, note: str = "") -> None:
    """_index.json is what a fresh chat reads first: every site, one line each, no directory listing.

    _health.json is the same shape but about the last run only, so a broken site is visible without reading
    a 40 MB log on another machine.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    idx = read_json_remote("_index.json") or {"sites": {}}
    idx.setdefault("sites", {})[site] = {
        "language": cfg["lang"], "url": cfg["base"],
        "method": cfg.get("chapter_style", "html"),
        "chapters": chapters, "novels_done": novels_done, "catalogue_size": novels_known,
        "path": f"{cfg['lang']}/{site}/", "updated": now,
    }
    idx["updated"] = now
    write_json_remote("_index.json", idx)

    health = read_json_remote("_health.json") or {"sites": {}}
    health.setdefault("sites", {})[site] = {
        "status": status, "note": note, "chapters": chapters,
        "catalogue_size": novels_known, "last_run": now,
    }
    health["updated"] = now
    write_json_remote("_health.json", health)


def push_novel(site: str, lang: str, workdir: Path, slug: str) -> None:
    """Copy ONE novel to Drive instead of the whole site.

    The old push rescanned every file under the site on every checkpoint: with 300 novels that is a full
    directory comparison against Drive every four minutes, and on this line it was the difference between
    ~10 chapters a minute and the site's own limit.
    """
    src = workdir / site / slug
    if src.exists():
        rclone("copy", str(src), f"{REMOTE}:novels/{lang}/{site}/{slug}",
               "--transfers", "4", "--checkers", "8", "--tpslimit", "8",
               "--ignore-existing", timeout=600)


def write_site_manifest(site: str, cfg: dict, workdir: Path, novels_known: int) -> None:
    """novels/<lang>/<site>/_site.json — what this folder is and how it was built."""
    src = workdir / site
    if not src.exists():
        return
    folders = [d for d in src.iterdir() if d.is_dir()]
    chapters = sum(sum(1 for _ in d.glob("*.txt")) for d in folders)
    short = 0
    for d in folders:
        try:
            idx = json.loads((d / "index.json").read_text(encoding="utf-8"))
            if idx.get("expected") and len(idx.get("chapters") or []) < idx["expected"]:
                short += 1
        except Exception:
            pass
    man = {
        "site": site, "language": cfg["lang"], "url": cfg["base"],
        "method": cfg.get("chapter_style", "html"),
        "text_selector": cfg.get("text_class") or "paragraph heuristic",
        "novels": len(folders), "chapters": chapters, "novels_short": short,
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
    migrate_legacy_file(f"{REMOTE}:novels/catalogue/{site}.json",
                        f"{REMOTE}:novels/{CATALOGUE_DIR}/{site}.json")
    rclone("copyto", f"{REMOTE}:novels/{CATALOGUE_DIR}/{site}.json", str(p))
    data = {"site": site, "built": None, "novels": [], "done": False, "next_page": 1}
    if p.exists():
        try:
            data.update(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    novels = data.get("novels") or []
    bad = cfg.get("catalogue_bad_re")
    if novels and bad:
        junk = sum(1 for u in novels if re.search(bad, u))
        if junk > max(5, len(novels) * 0.05):
            # This is what happens when the catalogue was built by a different rule: the entries are
            # chapter URLs, every one of them fetches a page with no chapter list, and the novels they
            # came from get recorded as "done". Throw the list away and rebuild it.
            log(f"[{site}] catalogue: {junk}/{len(novels)} entries are chapter URLs - rebuilding")
            novels = []
            data = {"site": site, "built": None, "novels": [], "done": False, "next_page": 1}
    if novels:
        log(f"[{site}] catalogue: {len(novels)} known, next page {data.get('next_page')}, done={data.get('done')}")

    c = cfg["catalogue"]
    if TOP_N and len(novels) >= TOP_N and not data.get("done"):
        data["done"] = True
        log(f"   catalogue: {len(novels)} known, cap {TOP_N} reached - no more crawling")

    def save():
        data.update({"novels": novels, "count": len(novels),
                     "built": time.strftime("%Y-%m-%d %H:%M:%S")})
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        rclone("copyto", str(p), f"{REMOTE}:novels/{CATALOGUE_DIR}/{site}.json")

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
            # Some APIs paginate by cursor only: novltime answers 400 to ?page=2 and hands out
            # meta.nextCursor instead. The cursor is stored in the catalogue so a resume continues there.
            cursor = data.get("cursor")
            if cursor:
                url = c["cursor_url"].format(cursor=cursor)
            else:
                # a stored next_page is meaningless for a cursor API, and asking for page 2 answers 400
                page = 1
                url = c["url"].format(page=1)
            while page <= c.get("max_pages", 500):
                if crawl_until and time.time() > crawl_until:
                    log(f"   catalogue: time up at page {page} (keeping {len(novels)})")
                    break
                raw = get(url)
                if not raw:
                    break
                try:
                    payload = json.loads(raw)
                    items = payload.get(c["items"], [])
                except Exception:
                    break
                urls = [tpl.format(value=it.get(c["slug"])) for it in items if it.get(c["slug"])]
                new = [u for u in urls if u not in seen]
                if not new and not c.get("cursor_url"):
                    data["done"] = True
                    log(f"   catalogue finished at page {page} (total {len(novels)})")
                    break
                novels.extend(new)
                seen.update(new)
                page += 1
                data["next_page"] = page
                if c.get("cursor_url"):
                    # A cursor API hands out every result in order: "no new novels on this page" just means
                    # this page overlapped what we already had, not that the list ended. Only a missing
                    # cursor ends it.
                    nxt = (payload.get("meta") or {}).get(c.get("cursor_field", "nextCursor"))
                    if not nxt:
                        data["done"] = True
                        log(f"   catalogue: no further cursor at page {page} (total {len(novels)})")
                        save()
                        break
                    data["cursor"] = nxt
                    url = c["cursor_url"].format(cursor=nxt)
                else:
                    url = c["url"].format(page=page)
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
def chapters_for(cfg: dict, novel_url: str, first_page: str) -> list[str] | None:
    """Return every chapter of a novel, following whatever the site uses to list them.

    Most novel sites show only the first ~30 chapters on the novel page, so a single fetch silently
    truncates the book. That was the bug that made every novel stop at 31 chapters.

    Returns None when the chapter-list page could not be fetched at all - the site throttled us, or the
    line dropped. That is different from [] which means the page arrived and holds no chapters, and the
    difference is what stops a bad hour from writing off a whole site.
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
        raw = post(novel_url.rstrip("/") + "/ajax/chapters/")
        if raw is None:
            return None
        found = links_in(raw)
        found.reverse()
        return sort_chapters(cfg, found)

    # numeric: the page renders only the newest ~120 links, but chapter URLs are chapter-{n}
    if style == "numeric":
        if not first_page:
            return None
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
        got = get(cfg["chapter_start"].format(slug=slug, novel=novel_url.rstrip("/")))
        if got is None:
            return None
        html = got
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
            hard_deadline: float | None = None, wait_min: int | None = None,
            threads: int = 8) -> None:
    # The home server keeps this on disk so a restart does not re-download chapters it already wrote;
    # a GitHub runner has nowhere to keep it and uses /tmp.
    workdir = Path(os.environ.get("HARVEST_WORK", "/tmp/work"))
    novel_dir = workdir / site
    novel_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + budget_min * 60
    if hard_deadline:
        deadline = min(deadline, hard_deadline)

    key = f"{site}-0of1"
    state = claim(key, workdir, wait_min=budget_min if wait_min is None else wait_min, force=force)
    if state is None:
        return
    migrate_legacy_lang(site, cfg["lang"])
    done = set(state.get("done_novels", []))
    crawl_until = time.time() + budget_min * 60 * 0.2
    if hard_deadline:
        crawl_until = min(crawl_until, hard_deadline)
    novels = catalogue(cfg, workdir, site, crawl_until)
    if TOP_N:
        novels = novels[:TOP_N]
    todo = [n for n in novels if n not in done]
    if limit_novels:
        todo = todo[:limit_novels]
    log(f"[{site}] catalogue {len(novels)} - todo {len(todo)} - budget {budget_min} min - "
                          f"{threads} threads, {cfg.get('rate', 6)} req/s cap")
    written = 0
    last_push = time.time()
    # HARVEST_RATE overrides every site's cap, so the data bill can be dialled down without touching code.
    # Each chapter page is ~55 KB while only ~6 KB is kept, so 6 req/s is roughly 1.2 GB an hour.
    limiter = Rate(float(os.environ.get("HARVEST_RATE", cfg.get("rate", 6))))
    write_lock = threading.Lock()
    counter = [0]
    blocked = 0

    for novel_url in todo:
        counter[0] = 0
        if time.time() > deadline:
            log(f"[{site}] budget reached")
            break
        cur = state.get("current") or {}
        start_from = cur.get("chapter", 1) if cur.get("novel") == novel_url else 1
        page = get(novel_url)
        if not page:
            continue
        slug = slugify(novel_url)
        folder = novel_dir / slug
        folder.mkdir(parents=True, exist_ok=True)
        list_cache = folder / "_links.json"
        links: list[str] = []
        if list_cache.exists() and time.time() - list_cache.stat().st_mtime < 7 * 86400:
            try:
                links = json.loads(list_cache.read_text(encoding="utf-8"))
            except Exception:
                links = []
        if not links:
            raw_links = chapters_for(cfg, novel_url, page)
            if raw_links is None:
                # The site did not answer the chapter list. That is not "this novel has no chapters" and it
                # must not count against the novel: an hour of throttling once wrote off 299 sunovels novels.
                blocked += 1
                log(f"[{site}] chapter list unavailable ({blocked} in a row): {novel_url[:70]}")
                if blocked >= 8:
                    log(f"[{site}] stopping this run - the site is not answering chapter lists")
                    state["current"] = None
                    state_save(key, state, workdir)
                    state["lease"] = {"owner": None, "until": 0, "beat": time.time()}
                    state_save(key, state, workdir)
                    push(site, cfg["lang"], workdir)
                    return
                time.sleep(min(60, 8 * blocked))
                continue
            blocked = 0
            links = chapter_links(cfg, novel_url, raw_links)
            if links:
                list_cache.write_text(json.dumps(links), encoding="utf-8", newline="\n")
        else:
            links = chapter_links(cfg, novel_url, links) if cfg.get("fetch_url") else links
        if start_from > len(links):
            # the chapter list shrank since the resume point was written; start this folder again
            start_from = 1
        # Never trust a resume point that runs ahead of the chapters we can actually account for. sunovels'
        # against-the-gods claimed chapter 1373 while only 50 files existed - the rest were written to
        # /tmp/work by an early build and lost with the container - so 1,300 chapters would have been
        # skipped for ever.
        if start_from - 1 > len(manifest):
            if start_from > 1:
                log(f"[{site}] {slug}: resume point {start_from} is ahead of {len(manifest)} known chapters - restarting from {len(manifest) + 1}")
            start_from = len(manifest) + 1
        if not links:
            # Do not record this as finished. A wrong catalogue used to cost whole novels here: each one
            # was marked done, never retried, and silently missing from the corpus.
            failed = state.get("failed") or {}
            failed[novel_url] = failed.get(novel_url, 0) + 1
            state["failed"] = failed
            if failed[novel_url] >= 3:
                log(f"[{site}] the site returned no chapters after 3 tries, writing off: {novel_url[:90]}")
                done.add(novel_url)
                state["done_novels"] = sorted(done)
            else:
                log(f"[{site}] no chapters found (try {failed[novel_url]}/3), will retry: {novel_url[:90]}")
            state["current"] = None
            state_save(key, state, workdir)
            continue
        manifest_path = folder / "index.json"
        manifest = {}
        if manifest_path.exists():
            try:
                manifest = {c["file"]: c for c in json.loads(manifest_path.read_text(encoding="utf-8")).get("chapters", [])}
            except Exception:
                manifest = {}
        remote_dir = f"{REMOTE}:novels/{cfg['lang']}/{site}/{slug}"
        if not manifest:
            # This machine has no record of this novel (a fresh workdir, or the other machine wrote it).
            # Without the remote index the "did this novel finish?" test only sees what this run wrote.
            try:
                remote_index = rclone("cat", f"{remote_dir}/index.json", timeout=30)
                manifest = {c["file"]: c for c in json.loads(remote_index).get("chapters", [])}
            except Exception:
                pass
        if start_from > len(manifest) + 1:
            # The resume point is ahead of what this machine can account for, so the files it is about to
            # skip are on Drive and nowhere else. List the folder - one call - instead of trusting a stale
            # index.json that a never-finished novel may not have.
            try:
                listing = rclone("lsf", remote_dir, "--files-only", timeout=60)
                for name in listing.splitlines():
                    name = name.strip()
                    if name.endswith(".txt") and name not in manifest:
                        try:
                            manifest[name] = {"file": name, "chapter": int(name[:4]), "title": ""}
                        except ValueError:
                            pass
            except Exception:
                pass
        if manifest:
            log(f"[{site}] {slug}: {len(manifest)} chapters already accounted for")
            manifest_path.write_text(json.dumps({"novel": slug, "url": novel_url,
                                                 "chapters": sorted(manifest.values(), key=lambda c: c["file"])},
                                                ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        # whatever this disk has is real even if Drive was ahead of it, so fold the local files in too
        for existing in folder.glob("*.txt"):
            if existing.name not in manifest:
                try:
                    first = existing.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0].strip()[:120]
                except Exception:
                    first = ""
                try:
                    manifest[existing.name] = {"file": existing.name, "chapter": int(existing.stem), "title": first}
                except ValueError:
                    pass

        # Eight chapters are in flight at once. The site still sees a steady ceiling (cfg['rate'] requests
        # per second, shared by every thread) instead of one request every second per thread.
        def fetch_one(i: int) -> bool:
            f = folder / f"{i + 1:04d}.txt"
            if f.exists():
                return False
            limiter.wait()
            raw = get_once(links[i]) if cfg.get("fetch_url") else get(links[i], tries=2)
            if not raw:
                return False
            title, body = chapter_body(cfg, raw)
            paras = [x for x in body.split("\n\n") if x.strip()]
            if len(paras) < 3:
                return False
            f.write_text((title or f"{slug} {i + 1}") + "\n\n" + "\n\n".join(paras) + "\n",
                         encoding="utf-8", newline="\n")
            with write_lock:
                manifest[f.name] = {"file": f.name, "chapter": i + 1, "title": (title or "")[:120]}
                counter[0] += 1
            return True

        # A rolling window, not batches: a finished chapter is replaced immediately, so no worker ever
        # sits idle waiting for the slowest member of its batch.
        stopped_at = None
        pending = {}
        idx = start_from - 1
        with ThreadPoolExecutor(max_workers=threads) as pool:
            while True:
                while (len(pending) < threads * 2 and idx < len(links) and time.time() <= deadline):
                    pending[pool.submit(fetch_one, idx)] = idx
                    idx += 1
                if not pending:
                    break
                # 'finished', not 'done': the outer 'done' is the set of finished novels, and shadowing it
                # here replaced it with completed futures -> "TypeError: '<' not supported between Future and str"
                finished, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                for fut in finished:
                    pending.pop(fut, None)
                    try:
                        fut.result()
                    except Exception as e:
                        log(f"[{site}] chapter error: {type(e).__name__}: {e}")
                if time.time() - last_push > 240:
                    manifest_path.write_text(json.dumps({"novel": slug, "url": novel_url,
                                                         "chapters": sorted(manifest.values(), key=lambda c: c["file"])},
                                                        ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
                    push_novel(site, cfg["lang"], workdir, slug)
                    state["current"] = {"novel": novel_url, "chapter": min(idx + 1, len(links))}
                    state_save(key, state, workdir)
                    last_push = time.time()
                    log(f"[{site}] pushed {counter[0]} so far (at {slug} ch{min(idx, len(links))})")
                if time.time() > deadline and not pending:
                    break
            if idx < len(links):
                stopped_at = idx

        written = counter[0]
        if stopped_at is not None:
            state["current"] = {"novel": novel_url, "chapter": stopped_at + 1}
            state_save(key, state, workdir)
            state["lease"] = {"owner": None, "until": 0, "beat": time.time()}
            state_save(key, state, workdir)
            push_novel(site, cfg["lang"], workdir, slug)
            total = sum(sum(1 for _ in d.glob("*.txt")) for d in novel_dir.iterdir() if d.is_dir())
            update_root_index(site, cfg, len(novels), total, len(done), "partial",
                              f"budget cut at {slug} chapter {stopped_at + 1}")
            log(f"[{site}] budget reached mid-novel {slug} at chapter {stopped_at + 1}")
            return

        manifest_path.write_text(json.dumps({"novel": slug, "url": novel_url,
                                             "chapters": sorted(manifest.values(), key=lambda c: c["file"])},
                                            ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        manifest_path.write_text(json.dumps({"novel": slug, "url": novel_url, "expected": len(links),
                                             "chapters": sorted(manifest.values(), key=lambda c: c["file"])},
                                            ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        have = len(manifest)
        if have < len(links) * 0.9:
            # half a novel is not a finished novel. Leave it out of done_novels so the next lap fills the gaps.
            log(f"[{site}] {slug}: only {have}/{len(links)} chapters written - will retry, not marking done")
            state["current"] = {"novel": novel_url, "chapter": 1}
            state_save(key, state, workdir)
            push_novel(site, cfg["lang"], workdir, slug)
            last_push = time.time()
            continue
        done.add(novel_url)
        state["done_novels"] = sorted(done)
        state["current"] = None
        state_save(key, state, workdir)
        push_novel(site, cfg["lang"], workdir, slug)
        last_push = time.time()
        log(f"[{site}] novel done: {slug} ({have}/{len(links)} chapters) - written this run {written}")

    push(site, cfg["lang"], workdir)
    write_site_manifest(site, cfg, workdir, len(novels))
    total = sum(sum(1 for _ in d.glob("*.txt")) for d in novel_dir.iterdir() if d.is_dir())
    update_root_index(site, cfg, len(novels), total, len(done), "ok")
    state["lease"] = {"owner": None, "until": 0, "beat": time.time()}
    state_save(key, state, workdir)
    log(f"[{site}] run finished - written {written} - novels done {len(done)}/{len(novels)} - {total} chapters on disk")


def run_all(sites: list[str], budget_min: int, force: bool, threads: int = 4) -> None:
    """Harvest several sites back to back inside one job, sharing the budget.

    The workflow matrix has one job per site. A site must never need a workflow-file edit (that needs a
    token scope this machine does not have), so each job also drains its deterministic slice of the other
    sites: the one it is named for first, then the rest. One owner per site still holds, because every
    site keeps its own lease.
    """
    # A GitHub job is the BACKUP. With the default 330-minute budget it held one site for nearly two hours
    # while the flat server stood by. It now never runs longer than two hours, and never more than 45
    # minutes on one site, so the primary gets the lease back quickly.
    hard = time.time() + min(max(budget_min - 6, 10), 120) * 60
    for i, site in enumerate(sites):
        left_min = int((hard - time.time()) / 60)
        if left_min < 10:
            log(f"[rotate] out of time, {len(sites) - i} sites left for the next run")
            break
        share = min(45, max(10, left_min // (len(sites) - i)))
        log(f"[rotate] {site}: {share} min of {left_min} left")
        try:
            harvest(site, SITES[site], share, force=force, hard_deadline=hard, wait_min=3, threads=threads)
        except Exception as e:
            log(f"[rotate] {site} failed: {type(e).__name__}: {e}")
            try:
                update_root_index(site, SITES[site], 0, 0, 0, "error", f"{type(e).__name__}: {e}"[:200])
            except Exception:
                pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True, choices=list(SITES))
    ap.add_argument("--budget-min", type=int, default=300)
    ap.add_argument("--limit-novels", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="take the lease even if another machine holds it")
    ap.add_argument("--threads", type=int, default=8, help="chapters fetched in parallel (GitHub runners handle 8)")
    ap.add_argument("--wait-min", type=int, default=0,
                    help="minutes to stand by for a lease before skipping the site (0 = use the budget)")
    ap.add_argument("--only", action="store_true",
                    help="harvest just this one site instead of this job's whole rotation slot")
    ap.add_argument("--all", action="store_true", help=argparse.SUPPRESS)  # kept so old commands still run
    args = ap.parse_args()

    if args.only or args.site not in SLOTS:
        harvest(args.site, SITES[args.site], args.budget_min, limit_novels=args.limit_novels,
                force=args.force, wait_min=args.wait_min or None, threads=args.threads)
        return
    slot = SLOTS[args.site]
    mine = [s for i, s in enumerate(ROTATION) if i % len(SLOTS) == slot]
    rotate = [args.site] + [s for s in mine if s != args.site]
    run_all(rotate, args.budget_min, args.force, threads=args.threads)


if __name__ == "__main__":
    main()
