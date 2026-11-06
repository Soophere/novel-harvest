# novels/ — the corpus, written for the machine that reads it

This folder is the only thing the flat server and GitHub Actions share. Both write here; neither can see
the other. Everything a human needs to know is in this file; everything a program needs is in two JSON
files at the root.

## Read these two first

    _index.json     every site: language, url, method, chapters, novels done, path, updated
    _health.json    per site: status, note, last_run, chapters — the quickest way to see what is broken

Nothing else needs listing. 200,000 files is not something to walk.

## Layout

    novels/
      README.md                     this file
      _index.json                   all sites, one object
      _health.json                  last run per site
      _state/<site>-0of1.json       lease + done_novels + current + failed
      _catalogue/<site>.json        the site's novel list as it grows
      arabic/<site>/<novel>/0001.txt    one chapter per file, numbered by position in the site's list
      arabic/<site>/<novel>/index.json  {novel, url, chapters:[{file, chapter, title}]}
      arabic/<site>/_site.json          {site, language, url, method, novels, chapters, built}
      english/<site>/...            the same shape for the English sources

Machine-owned names start with an underscore so they sort above the corpus in any listing.

## The one rule

**Only one machine may work a site at a time**, proven by the lease in `_state/<site>-0of1.json`:

    {"lease": {"owner": "docker:2593474", "until": 1793900000, "beat": 1793899000},
     "done_novels": ["https://..."], "current": {"novel": "https://...", "chapter": 452},
     "failed": {"https://...": 1}}

- lease life 20 minutes, renewed on every save (~every 4 minutes)
- the other machine polls and takes over when it expires, continuing from `current.chapter`
- a clean exit releases it immediately, including when the budget cuts a novel short
- a lease whose host is this machine and whose pid is gone is reclaimed at once
- all writes are `rclone copy --ignore-existing`, never `sync`

**flat server = primary.** It runs `harvest-runner` and owns every site while its line is up.
**GitHub Actions = backup.** It only gets a site the flat server is not holding.

## What is in here

| language | site | method | chapters verified |
|---|---|---|---|
| arabic | kolnovel | HTTP + WordPress sitemap | 143,579 (481 novels) |
| arabic | galaxynovels | `?chapter_page=N`, sorted by chapter number (site lists newest-first) | 3,236 |
| arabic | novltime | API catalogue + `chapter-{n}` up to "الفصل 1 من N" | 750 |
| arabic | sunovels | `?activeTab=chapters` (page 1 is its own URL) | 1,537 |
| english | wuxiadreams | `/novel/slug?page=N`, 100 per page | 2,720 |
| english | fanmtl | `/e/extend/fy.php?page=N&wjm=slug` | 1,736 |
| english | wuxiaworld.site | POST `<novel>/ajax/chapters/` (Madara) | 3,864 |
| english | novelfire | `/book/<slug>/chapters?page=N`; text read from novelphoenix.com | 869 |

Dropped on purpose: **mknov.com** (its text container returns mixed-script garbage) and **novelcool.com**
(it rewrites every chapter link to an ad redirect unless JavaScript runs).

## Chapter files

`0001.txt` is the first chapter of the novel, not the newest. A gap in the numbering means that chapter was
locked or missing on the source, not an error. The first line of each file is the chapter title, the rest is
the story, one paragraph per line, separated by blank lines, always written with `\n`.

## What this feeds

- **Pipeline A**: English source → Arabic translation → rewrite → publish to cenele.com
- **Pipeline B**: an existing Arabic translation plus the English source → better Arabic
  (`tools/improve-pass.py`, about $0.013 a chapter, review sheet per chapter)

Repo: https://github.com/Soophere/novel-harvest (public).
