"""top10.py — the top ten novels of every site, written into the Obsidian vault.

    python tools/top10.py                 # refresh the note
    python tools/top10.py --top 25        # more than ten

The list comes from the same listing the harvester reads, so it matches what will actually be downloaded
first. Where a site offers a real popularity sort it is used: novelfire's sort-popular and fanmtl's
all-onclick. The rest follow the site's own default order.
"""

import sys, json, re, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "harvest")
import runner

# the same listing each site's harvester reads, so the list matches what will actually be downloaded
PAGES = {
    "galaxynovels": ("https://galaxynovels.com/novels/?page=1", r'href="(https://galaxynovels\.com/novel/[^"/]+/)"'),
    "novltime":     ("https://novltime.com/api/v1/novels?page=1", None),
    "sunovels":     ("https://sunovels.com/library?page=1", r'href="(/novel/[^"/?#]+)"'),
    "wuxiadreams":  ("https://wuxiadreams.com/novels?page=1", r'href="(/novel/[a-z0-9\-]+)"'),
    "fanmtl":       ("https://www.fanmtl.com/list/all/all-onclick-1.html", r'href="(/novel/[a-z0-9\-]+\.html)"'),
    "wuxiaworldsite": ("https://wuxiaworld.site/novel/page/1/", r'href="(https://wuxiaworld\.site/novel/[a-z0-9\-]+/)"'),
    "novelfire":    ("https://novelfire.net/genre-all/sort-popular/status-all/all-novel?page=1", r'href="(/book/[a-z0-9\-]+)"'),
}

def title_of(url):
    raw = runner.get_once(url) or ""
    for pat in (r'<meta property="og:title" content="([^"]+)"', r"<title[^>]*>(.*?)</title>"):
        m = re.search(pat, raw, re.S)
        if m:
            t = re.sub(r"\s+", " ", m.group(1)).strip()
            t = re.sub(r"\s*[\|\-–]\s*(Novel Cool|Novel Fire|WuxiaWorld.*|Fanmtl.*|Wuxia Dreams.*|NovelTime.*|شمس الروايات.*|ملوك الروايات.*|Galaxy Novels.*)$", "", t, flags=re.I)
            return t[:120]
    return ""

result = {}
for site, (url, pat) in PAGES.items():
    cfg = runner.SITES[site]
    raw = runner.get(url) or ""
    if pat is None:
        try:
            items = json.loads(raw).get("data", [])[:10]
            urls = [f"https://novltime.com/ar/novel/{it['slug']}" for it in items]
        except Exception as e:
            print(site, "json fail", e); urls = []
    else:
        found = []
        skip = cfg["catalogue"].get("skip_re")
        for h in re.findall(pat, raw):
            if skip and re.search(skip, h):
                continue
            h = h if h.startswith("http") else cfg["base"] + h
            if h not in found:
                found.append(h)
        urls = found[:10]
    with ThreadPoolExecutor(max_workers=8) as ex:
        titles = list(ex.map(title_of, urls))
    result[site] = [{"title": t, "url": u} for t, u in zip(titles, urls)]
    print(f"{site:16s} {len(urls)} novels", flush=True)
    for t, u in zip(titles, urls):
        print(f"   {t[:70]:72s} {u[:60]}")

VAULT = r"C:\Users\Sulaiman\Documents\Obsidian Vault\الروايات\أفضل 10 روايات من كل موقع.md"
json.dump(result, open(".tmp-top10.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("saved .tmp-top10.json - run tools/write-top10-note.py to refresh the vault note")
