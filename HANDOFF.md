# NOVEL PIPELINE — HANDOFF (read this first)

You are taking over a live project. Everything below is verified state, not a proposal. Read it fully before
touching anything, then follow "Next steps" in order.

---

## 0. TL;DR — what this project is

Sulaiman translates web novels into Arabic and publishes them on **cenele.com** (فضاء الروايات). To get source
material, the project harvests **entire websites** of novels (Arabic and English) into Google Drive, then:

1. **Pipeline A (existing, working):** English source → Arabic translation → rewrite → publish to cenele.
2. **Pipeline B (new, not built):** for novels that already have an Arabic translation, take that translation
   **plus** the English source and produce a *better* Arabic version (cheaper and faster than translating
   from scratch).

The harvest feeds both. It must run unattended for weeks.

---

## 1. Repositories and machines

| what | where |
|---|---|
| **harvest repo (GitHub)** | `https://github.com/Soophere/novel-harvest` (public, unlimited Actions minutes) |
| **local project** | `D:\were i waste my time\novelTranslation` |
| **home server** | Proxmox host `pve`, ssh alias `home-zt` (ZeroTier `10.36.10.172`), container **CT 100** |
| **server data** | `/opt/harvest/` (novels, catalogues, state, logs), rclone remote `gdrive` |
| **Google Drive** | account `sulaimangantlman@gmail.com` (Google AI Pro via Jio, **5 TB**), root folder `novels/` |

The home server's internet is **unreliable** (drops for hours). That is why GitHub Actions exists: it is the
reliable worker, the server is the opportunistic one.

---

## 2. THE ONE ARCHITECTURAL RULE (do not break it)

> **Only one machine may harvest a given site at a time.**

This is enforced by a **lease in Google Drive**, not by hope:

- state file per site: `novels/state/<site>-0of1.json`
- `{"lease": {"owner": "<host>:<pid>", "until": <epoch>, "beat": <epoch>}, "done_novels": [...], "current": {...}}`
- a machine may work the site only if it holds the lease; it **waits and polls every 3 minutes** otherwise
- the lease expires **20 minutes** after the last renewal (renewed on every save, ~every 4 minutes)
- on a clean exit the lease is released
- `--force` overrides it (use only when you know the other machine is dead)

**Required behaviour:** flat server works while its line is up → it dies → GitHub takes the lease within ~20 min
and continues **from the same chapter** → server returns → it stands by until GitHub's job ends or stalls.
Never two workers on one site. Never re-fetch what is already on Drive.

---

## 3. What is already harvested (verified numbers, 2026-11-05)

**On the home server** (`/opt/harvest/novels/arabic/...`):

| site | novels | files | depth |
|---|---|---|---|
| kolnovel | 481 | 143,579 | full (sitemap-driven, 550 chapters/min) |
| novltime | 1,036 | 64,250 | **unverified** (~62 chapters/novel — may be truncated) |
| galaxynovels | 260 | 8,552 | **TRUNCATED — only 31 chapters per novel, must be deleted and redone** |

Truncated remnants (safe to delete once confirmed): `novels/candidates/{ar,en}` (20 old sample novels).

**On Google Drive** `novels/`:

```
novels/
  arabic/<site>/<novel>/NNNN.txt + index.json
  english/<site>/<novel>/NNNN.txt + index.json
  catalogue/<site>.json        # the site's novel list {site, built, count, novels:[urls]}
  state/<site>-0of1.json       # lease + progress (see §2)
```

Catalogues already built: `galaxynovels` 613 · `wuxiaworldsite` 9,981 · `fanmtl` ~17,992 (crawl incomplete at
the time of writing) · `novltime` 1,259 · `kolnovel` 342 (sitemap) — note the server holds **481** kolnovel
novels, so the sitemap was not the whole site.

---

## 4. The 10 target sites and their true status

| # | site | lang | method that works | status |
|---|---|---|---|---|
| 1 | kolnovel.com | ar | **plain HTTP + 32 threads**, chapter text in `.entry-content`, chapter URLs from `wp-sitemap.xml` (70 files → 119k URLs) | ✅ done on server |
| 2 | novltime.com | ar | **browser only**: client-rendered, its API (`/api/v1/...`) answers 401 to anonymous callers; catalogue via `GET /api/v1/novels?page=1` then `meta.nextCursor` | partial, depth unverified |
| 3 | galaxynovels.com | ar | **plain HTTP**, text in `<article>`, chapter list **paginated** with `?chapter_page=N` | works after the pagination fix |
| 4 | novelfire.net | en | JS challenge on plain HTTP → needs a browser, ~6 s/chapter | only 192 chapters so far |
| 5 | wuxiaworld.site | en | plain HTTP, `.entry-content`; catalogue from `sitemap.xml` (9,981 novels) | wired, depth unverified |
| 6 | fanmtl.com | en | plain HTTP, `.chapter-content`; catalogue by crawling `/list/all/all-onclick-{page}.html` (30/page, ~600 pages); chapters `/novel/<slug>_<n>.html`, pager `/e/extend/fy.php?page={n}&wjm={slug}` | wired, depth unverified |
| 7 | novelcool.com | en | plain HTTP, chapter URLs in the sitemap; blocks bursts | not wired |
| 8 | mknov.com | ar | HTTP works but the text container returns **garbage (mixed scripts)** — treat as unusable until proven | not wired |
| 9 | sunovels.com | ar | JS-rendered | not wired |
| 10 | wuxiadreams.com | en | JS-rendered chapter list | not wired |

### THE BIG SHORTCUT — use a maintained crawler instead of writing adapters

`lightnovel-crawler` (PyPI, v4.14.0, ~300 sources, actively maintained) already supports these of ours:

```
kolnovel.com · sunovels.com · fanmtl.com · novelcool.com · novelfire.net · wuxiaworld.site
```

Install: `python -m pip install lightnovel-crawler`
CLI: `python -m lncrawl crawl <novel-url>` · `python -m lncrawl search "<title>"` · `python -m lncrawl sources list`

**Before writing any more site adapters, evaluate `lncrawl` as the download engine.** It handles pagination,
Cloudflare-ish tricks and per-source quirks. Our runner would become: catalogue → for each novel URL call
lncrawl's API → write the chapters through our existing writer (so resume/lease/Drive logic is unchanged).

---

## 5. The harvester (current implementation)

`harvest/runner.py` in the repo (also `/opt/harvest/runner.py` on the server). One file, stdlib only, plus
`harvest/extract.py` (HTML → story text).

Key functions:

- `claim(key, workdir, wait_min, force)` — the lease (§2). Returns fresh state or `None`.
- `catalogue(...)` — loads `novels/catalogue/<site>.json` from Drive; if incomplete it crawls **at most 20 % of
  the budget**, saving `next_page` every 25 pages, then stops. Never shrinks a good catalogue.
- `chapters_for(cfg, novel_url, first_page)` — **follows chapter-list pagination**. This was the bug that made
  every novel stop at 31 chapters.
- `harvest(...)` — loop novels → chapters → write `NNNN.txt` + `index.json`; push to Drive every ~4 minutes;
  update state; handle budget deadline; release lease at the end.
- `push(...)` — `rclone copy` (never `sync`: `sync` could delete another machine's files), `--ignore-existing`.

Site config lives in the `SITES` dict at the top: `lang`, `base`, `catalogue`, `chapter_re`, `chapter_pager`,
`chapter_style` (`sequential` for fanmtl), `text_class`, `delay`.

### GitHub workflow — `.github/workflows/harvest.yml`

- cron `20 */6 * * *` (plus manual `workflow_dispatch` with `site` and `budget` inputs)
- matrix: one job per site (`galaxynovels`, `fanmtl`, `wuxiaworldsite`) — **never shard a site across jobs**,
  that breaks §2
- each job: checkout → install rclone → write `~/.config/rclone/rclone.conf` from secret **`RCLONE_CONF`** →
  `PYTHONPATH=harvest python3 harvest/runner.py --site X --budget-min 330`
- job timeout 355 min

**Gotcha:** the `gh` CLI token on the laptop has scopes `gist, read:org, repo` — **not `workflow`**. So workflow
file changes must be made through the GitHub web UI, or fix it once with
`gh auth refresh -h github.com -s workflow` (device flow, needs the browser).

---

## 6. Bugs already found and fixed (do not reintroduce them)

1. **Chapter-list pagination** — every site shows ~30 chapters on page 1; without following `?chapter_page=N`
   you silently harvest 31 chapters of a 3,235-chapter novel. Symptom: "every novel has exactly 31 chapters".
2. **Pagination 404 = last page** — retrying 5× per novel wasted ~40 s each. Use a single attempt (`get_once`).
3. **`written` / `last_push` lost** during a refactor → `UnboundLocalError` and every job crashed on chapter 1.
4. **`state_load`/`state_save` parameter renamed** but the body still used the old name → `NameError`.
5. **rclone tries IPv6 first**; the container has no IPv6 route → "network is unreachable". Always pass
   `--bind 0.0.0.0`.
6. **Google Drive rate-limits thousands of small files** → keep `--transfers 4 --checkers 8 --tpslimit 8`,
   and expect `rateLimitExceeded` in manual API calls (this is normal, not a bug).
7. **Long lease (6 h) blocked the other machine** after a job finished. Lease is now 20 min and released on exit.
8. **Different state keys on the two machines** (`of=1` vs `of=2`) let both work a site unnoticed. Both must use
   `--of 1` / one key per site.
9. **Fanmtl catalogue is ~18,000 novels**: a full crawl costs more than one job. That is why the crawl budget is
   20 % and the cursor persists.
10. **Truncated data must be deleted, not merged** — file numbers map to positions in the chapter list; if the
    list was wrong, the whole folder is wrong.

---

## 7. Fast file access (I am the one who reads these files)

The library is ~200k tiny files. Do **not** grep it.

```bash
# build / refresh the index (SQLite FTS5)
python tools/index-library.py --root novels --db library.db --incremental

# query
python tools/find.py "كانغ شو" --limit 5
python tools/find.py --site galaxynovels --novel shadow-slave --chapter 12
python tools/find.py --stats
```

`tools/find.py` returns site/novel/chapter + a highlighted snippet, which is also how you **compare the same
passage across two translations** (Pipeline B needs this).

---

## 8. The publishing side (cenele) — different from harvesting, do not mix them up

- Site: WordPress + Madara; translated chapters are posted through `wp-admin/admin-post.php` with action
  `nhv_td_submit_chapter` (see `tools/publish-n2.py` for a working implementation).
- Login needs a **visible** Chrome: `--remote-debugging-port=9334 --user-data-dir=%LOCALAPPDATA%\dsh-chrome-cenele`
  (Cloudflare Turnstile refuses headless).
- Scheduling uses **site time = UTC+1**, so 23:30 Oman = 20:30 site.
- **Numbering trap:** the number inside a file is the *source* number; the number on cenele is `143 + file index`
  (knight novel). Always map by index, never by title.
- Novels in play: **Eternally Regressing Knight** (cenele id 85719), **Infinite Bloodcore** (id 7715).
  Blood is fully published 305→345.
- **Never touch cenele chapters 1–257** (another translator's work).

## 9. Translation rules (Pipeline A) — these are his own instructions

- Common Arabic words over dictionary words; convey the **image**, not the words.
- Read the English twice, self-review 3×, **flag doubts instead of guessing**.
- Two-pass method: translate, then **rewrite from the English** (paragraph-level prompts; chapter-level makes
  the model copy). Verified: «كان/كانت» dropped 35.3 → 23.0 per 1000 words, 50 % of paragraphs changed.
- Locked names: `Human Race youth → فتى البشر`, `Fishman youth → فتى السمك`.
- Glossary `novels/infinite-bloodcore/glossary.json`, rules `rules.md`, review sheets per batch
  (sexual content, profanity, gender/pronoun, names, English alongside, correction column).
- Costs (DeepSeek `deepseek-chat`): ~$0.003/chapter for the improve pass, ~$2.4 for a whole novel.
  Balance check: `python tools/balance.py`.
- **Never spend money without telling him first.** He is sensitive to silent token burn.

---

## 10. Invariants (hard rules)

1. **Never touch** the Pigeon containers, the reading site service, or the novel service on the server.
2. **Never publish to cenele without explicit approval.**
3. **One worker per site** (§2). `rclone copy`, never `sync`, for anything a second machine can write.
4. **Never delete harvested data** without confirming it is a truncated/duplicate copy (§6.10).
5. Keep the repo **public** (unlimited Actions minutes); secrets live in repo secrets, never in code.
6. All Chinese/Arabic text files are written with `newline="\n"`; on Windows Python defaults to CRLF and the
   paragraph splitting silently breaks.

---

## 11. Next steps (do them in this order)

1. **Verify depth on the three wired sites.** Open `novels/english/fanmtl/<novel>/` and
   `novels/english/wuxiaworldsite/<novel>/` in Drive. If a novel has ~30 chapters, its `chapter_pager` is wrong
   — fix `SITES` and re-harvest that site after deleting the bad folders.
2. **Delete the truncated galaxynovels data** (`/opt/harvest/novels/arabic/galaxynovels` on the server and
   `novels/arabic/galaxynovels` in Drive), reset `state/galaxynovels-0of1.json`, let it re-harvest.
3. **Evaluate `lncrawl`** for novelfire, novelcool, sunovels (three of the four unwired sites it supports).
   Prefer it over new hand-written adapters.
4. **Add the remaining sites** one at a time, each proven on **one full novel** before running wide:
   novltime (browser), novelfire, novelcool, sunovels, mknov, wuxiadreams.
5. **Verify novltime depth** (~62 chapters/novel looks low) and fix if truncated.
6. **Build Pipeline B**: for novels present in both languages, produce an improved Arabic version.
   Start with 5 novels, produce before/after review sheets, get his corrections, then scale.
7. **Label Drive properly**: a `README.md` at `novels/` describing the tree, and a `_site.json` per site
   `{site, language, url, method, novels, chapters, built}`.
8. **Watch disk**: server had ~35 GB free. Only if it gets tight, remove the Minecraft container (CT 101,
   `hostcraft`) — it is stopped and unused for this project.

---

## 12. How to check state in one command

On the server:

```bash
pct exec 100 -- python3 /opt/harvest/srv-status.py      # novels/files per site + catalogues
pct exec 100 -- systemctl is-active harvest-worker harvest-novltime harvest-galaxy harvest-sync.timer
pct exec 100 -- tail -n 40 /opt/harvest/out/worker.log
```

On GitHub:

```bash
gh run list --repo Soophere/novel-harvest --limit 5
gh run view <run-id> --repo Soophere/novel-harvest --log
gh api repos/Soophere/novel-harvest/actions/jobs/<job-id>/logs --allow-escape-sequences | tail -40
```

---

## 13. Things he asked for that are NOT done yet (in his words)

- "harvest all novels from all 10 sites" → **3 of 10 wired, 1 done**
- "the flat server is main, GitHub takes over when it dies" → **built (lease), needs both machines deployed**
- "store it all in Google Drive" → works for every site the runner handles
- "everything labelled correctly for future use" → folder layout is good; **Drive has no README/manifests yet**
- "check the best/fastest method per site" → done for kolnovel (HTTP+threads, 550/min); others not measured
- "don't redo work already done" → resume-by-file-existence does this; truncated folders are the exception
```
