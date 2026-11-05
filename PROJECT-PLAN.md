# PROJECT PLAN — from "two novels" to "a library"

Status as of 2026-11-06. Phases are ordered; each has a hard definition of done.

---

## Phase 0 — Foundations (DONE)

| item | evidence |
|---|---|
| Home server runs harvest as systemd services that survive reboots/network loss | `harvest-worker`, `harvest-novltime`, `harvest-sync.timer` |
| kolnovel fully harvested | 481 novels · 143,579 files · HTTP+threads at 550 chapters/min |
| Google Drive wired (5 TB) with rclone, IPv4 forced, rate-limit-safe settings | `rclone about gdrive:` = 5 TiB |
| GitHub Actions worker with the same code | `Soophere/novel-harvest`, public, cron every 6 h |
| One-worker-per-site lease in Drive | `state/<site>-0of1.json` |
| Chapter-list pagination | test: 168 / 112 / 144 chapters per novel instead of 31 |

---

## Phase 1 — Finish the harvest of all 10 sites (IN PROGRESS)

Definition of done: every site has `novels/<lang>/<site>/<novel>/NNNN.txt`, a `_site.json`, and a catalogue in
`novels/catalogue/`; no truncated folders remain; schedule runs unattended.

| step | site | method | state |
|---|---|---|---|
| 1.1 | galaxynovels | HTTP + `?chapter_page=N`, sorted by chapter number | ✅ re-harvesting (3,236-chapter novel verified) |
| 1.2 | fanmtl | HTTP + `/e/extend/fy.php` pager | ✅ verified 1,736 chapters |
| 1.3 | wuxiaworld.site | POST `<novel>/ajax/chapters/` (Madara) | ✅ verified 3,864 chapters |
| 1.4 | novelfire | HTTP, text from the novelphoenix.com mirror | ✅ verified 869 chapters |
| 1.5 | novelcool | chapter list is JavaScript | ❌ still open |
| 1.6 | sunovels | HTTP, `?activeTab=chapters` | ✅ verified 1,537 chapters |
| 1.7 | novltime | HTTP, API catalogue + `chapter-{n}` enumeration | ✅ verified 750 chapters |
| 1.8 | mknov | unusable text output — dropped on purpose | ⛔ dropped |
| 1.9 | wuxiadreams | HTTP Astro site, 100 chapters per page | ✅ verified 1,200 chapters |

Nine of ten sites are wired (mknov dropped, novelcool open). Nothing needs a browser.
Every site is proven by `python tools/verify-site.py --site <s> --url <novel>` before it runs wide.

**Rule:** a site is "done" only when one novel has its full chapter count, verified against the site.

---

## Phase 2 — Labels and searchability (SMALL, DO FIRST AFTER PHASE 1)

- ✅ `novels/README.md` (tree + conventions + the lease rule) is uploaded to Drive
- ✅ `novels/<lang>/<site>/_site.json` `{site, language, url, method, novels, chapters, built}` is written
  by the runner at the end of every successful site run
- `python tools/index-library.py --root novels --db library.db` → SQLite FTS5 index
- `python tools/find.py "…"` for searching passages across every translation

Definition of done: from a fresh chat, one command finds any passage and tells you which site/novel/chapter it
came from.

---

## Phase 3 — Pair novels across languages (ENABLES PIPELINE B)

- ✅ `tools/build-pairs.py` — 150 pairs found (fanmtl English × galaxynovels Arabic)
- ✅ Output `pairs.json` `{novel, en:[{site,url}], ar:[{site,url}]}`
- Next: add novltime and sunovels catalogues to the input so the Arabic side is not only galaxynovels
- Output a report: matched / only-EN / only-AR, with chapter counts on each side.
- Definition of done: a list of N novels where both texts exist, with coverage %.

---

## Phase 4 — Pipeline B: improve an existing Arabic translation

Method (his idea): give the model the published Arabic chapter **and** the English source, ask for an improved
Arabic version that keeps his voice rules.

- ✅ `tools/improve-pass.py`: paragraph chunks of 6 against the matching English, `style-rules.json`
  applied, one review pass against the English, review sheet + metrics per chapter
- ✅ Proven on Shadow Slave ch1 (wuxiadreams EN × novltime AR): 97→97 paragraphs, 89 changed (92 %),
  «كان/كانت» 26.3→24.5 per 1000 words, **$0.0152** measured from the API usage — about 5× the $0.003 target,
  because it reads the English and writes Arabic twice per chapter
- Open: 5 chapters of one pair, his corrections, then scale
- Original step, still valid: **5 chapters from one paired novel**, produce a before/after review sheet
  (his correction column, per `tools/review-n2.py` format)
- Feed his corrections into `LESSONS.md` / glossary
- Cost target: ~$0.003/chapter; a 300-chapter novel ≈ $1
- Definition of done: he reads a before/after sheet and says "this is better" — then scale to the paired list

---

## Phase 5 — Publishing (Pipeline A, existing)

- Translate → rewrite → QA → review sheet → he approves → publish to cenele
- Blood novel: complete through 345. Knight: complete (838 chapters live)
- Next novel choice comes from Phase 3 output (prefer one with an existing Arabic translation → Pipeline B)
- **Never publish without explicit approval.** Never touch cenele chapters 1–257.

---

## Phase 6 — Durability (ongoing)

- Weekly: check `gh run list` for failures; check disk (`df -h /`)
- Monthly: verify Drive state files exist per site; prune truncated/duplicate folders
- If the home line stays bad: consider the free Google Cloud `e2-micro` VM (needs a real bank card; Oracle
  rejected his prepaid card) as a second always-on worker

---

## What "done" looks like for the whole project

```
10 sites harvested → indexed → paired → improved Arabic generated → reviewed by him → published on cenele
```

with the harvest running unattended, one worker per site, everything in Google Drive, and any chat able to pick
up the work from `HANDOFF.md`.
