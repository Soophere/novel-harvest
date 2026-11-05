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

## 0.5 STATUS 2026-11-06 — read this before the rest

The harvester was rewritten around what the live sites actually do. Everything below in §§3–5 that
disagrees with this block is out of date.

**What changed**

1. **All eight remaining sites now have a working method, none of them needs a browser.** novltime and
   sunovels turned out to be plain HTTP once the chapter list URL was found (the novel page only renders
   120 chapters on novltime and "first / last" links on sunovels). wuxiaworld.site needs one POST to
   `<novel>/ajax/chapters/`, which is a Madara theme endpoint and returns all 3,864 chapters at once.
   novelfire serves a JavaScript stub and the real chapter lives on **novelphoenix.com** — a URL rewrite,
   not a browser. wuxiadreams is a plain Astro site, 100 chapters per page.
2. **Paragraph extraction replaced class guessing.** `extract.best_paragraphs()` groups every `<p>` by
   its nearest ancestor carrying a class and takes the richest group. Menus, breadcrumbs and Prev/Next
   rows are links and list items, so they never enter the paragraph set. Verified clean on fanmtl,
   wuxiadreams, wuxiaworld.site, novltime, novelfire and sunovels (whose content class carries hex
   watermarks — paragraphs do not).
3. **galaxynovels listed newest-first**, so 0001.txt was chapter 3236. `chapter_sort` now sorts by the
   number in the URL. The reversed 260-novel copy was deleted from Drive and the server (163 MB) and its
   state was reset; it is re-harvesting from scratch in reading order.
4. **A job now drains a rotation slot.** The `gh` token still has no `workflow` scope, so the workflow
   file cannot be edited from this machine. Instead `runner.py` defaults to harvesting its own site and
   then its share of the other six, so adding a site never needs a workflow change:
   slot 0 = galaxynovels, wuxiadreams, novelfire · slot 1 = fanmtl, novltime · slot 2 = wuxiaworldsite,
   sunovels. kolnovel is finished and out of the rotation.
5. **Drive is labelled**: `novels/README.md` describes the tree, and every site folder gets a
   `_site.json` with its site, language, URL, method, novel count and chapter count.
6. **The old server workers are stopped.** `harvest-novltime` and `harvest-galaxy` were disabled (they
   wrote without the lease and would have collided with GitHub now that novltime is GitHub's site).
   `sync-drive.sh` used `rclone sync`, which would delete everything GitHub wrote; it is `copy` now.
7. **Pipeline B is built and proven** on one chapter: `tools/improve-pass.py` fetches the English and
   the existing Arabic, improves paragraph-by-paragraph (6 per prompt, aligned against the English),
   applies `style-rules.json`, runs one review pass against the English, and writes a review sheet plus
   metrics. Measured: 97 → 97 paragraphs, 89 changed (92 %), «كان/كانت» 26.3 → 24.5 per 1000 words,
   **$0.0152** for the chapter. `tools/build-pairs.py` found **150 English↔Arabic pairs** between
   fanmtl and galaxynovels.

**Honest remaining gaps**

- novelcool.com is still unwired: its chapter list is assembled in JavaScript and its sitemap only
  carries chapter URLs, not the mapping to a novel.
- mknov.com is dropped on purpose — its text container returns mixed-script garbage.
- Depth per site is proven by `tools/verify-site.py` (galaxynovels 3,236 · wuxiaworld.site 3,864 ·
  novltime 750 · fanmtl 1,736 · wuxiadreams 1,200 · novelfire 869 · sunovels 1,537 chapters on the
  novels tested). The catalogue crawls for fanmtl/wuxiaworldsite are still incomplete by design.

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

| # | site | lang | method that works (all plain HTTP, no browser) | status |
|---|---|---|---|---|
| 1 | kolnovel.com | ar | HTTP + 32 threads, text in `.entry-content`, chapter URLs from `wp-sitemap.xml` | ✅ done on server, 481 novels |
| 2 | novltime.com | ar | catalogue from the public API `/api/v1/novels?page=N` (1,259 novels); the novel page renders only 120 chapters, so `chapter-{n}` is enumerated 1..max | ✅ verified 750 chapters |
| 3 | galaxynovels.com | ar | HTTP, text in `<article>`, chapter list paginated with `?chapter_page=N` — **newest first**, sorted by the number in the URL | ✅ verified 3,236 chapters |
| 4 | novelfire.net | en | chapter list at `/book/<slug>/chapters?page=N` (100/page); the chapter page is a JS stub, the text is on **novelphoenix.com/novel/<slug>/chapter-{n}** | ✅ verified 869 chapters |
| 5 | wuxiaworld.site | en | **POST `<novel>/ajax/chapters/`** (Madara) returns every chapter in one response; catalogue from `/novel/page/{n}/` | ✅ verified 3,864 chapters |
| 6 | fanmtl.com | en | catalogue `/list/all/all-onclick-{page}.html`; chapters `/novel/<slug>_<n>.html`, pager `/e/extend/fy.php?page={n}&wjm={slug}` | ✅ verified 1,736 chapters |
| 7 | novelcool.com | en | chapter list is assembled in JavaScript; the sitemap has chapter URLs but no novel mapping | ❌ not wired |
| 8 | mknov.com | ar | text container returns mixed-script garbage | ❌ dropped |
| 9 | sunovels.com | ar | `/library?page=N` catalogue; chapter list at `?activeTab=chapters` (**page 1 is its own URL**); locked chapters return one sentence, they are skipped | ✅ verified 1,537 chapters |
| 10 | wuxiadreams.com | en | Astro site, `/novels?page=N` catalogue, `/novel/<slug>?page=N` chapter list, 100/page | ✅ verified 1,200 chapters |

### lightnovel-crawler (`lncrawl`) — evaluated, not adopted

`lncrawl` 4.14 was installed and tested against the sites it claims: kolnovel, sunovels, fanmtl, novelcool,
novelfire, wuxiaworld.site.

| site | lncrawl result |
|---|---|
| wuxiaworld.site | works (3,866 chapters found) — but only because its Madara template POSTs to `/ajax/chapters/`, which is 6 lines to copy |
| sunovels | works, slowly (its template also revealed `?activeTab=chapters` and the `svg.fa-lock-open` marker) |
| novelcool | lists chapters, but chapter 1 came back with an empty body |
| novelfire | **broken**: its adapter builds `readnovel.site/novel/x/\/novelphoenix.com\/...` and every chapter 404s |
| fanmtl | worked, but no better than 20 lines of our own |

Verdict: it was worth one afternoon — it is how the sunovels and Madara methods were found — but it is a
300-source, SQLite, async, 40-dependency package to drive from a stdlib runner, and it failed on 2 of 6.
The runner keeps its own adapter per site. Re-check it first if a site breaks.

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

Done on 2026-11-06: depth verification for every wired site, the galaxynovels cleanup and re-harvest,
the `lncrawl` evaluation, all eight sites wired, novltime depth, the Drive README and `_site.json`,
and a working Pipeline B with one chapter behind it.

1. **Let the rotation finish a full pass** and then read the `_site.json` counts per site. Any site whose
   *chapters per novel* is far below the verified numbers in §0.5 has a bug in its chapter list, not a
   short catalogue.
2. **Run Pipeline B on 5 chapters of a real pair** (`python tools/improve-pass.py --pair chrysalis ...`),
   send him the review sheets, and take his corrections before scaling. The method is proven; the voice
   is not yet his.
3. **Pipeline B at scale** needs the chapters from Drive, not from the sites: index the library first
   (`tools/index-library.py`), then improve from the local text so a re-run costs nothing in requests.
4. **novelcool**: the last unwired site. Its chapter list is built in JavaScript; look for the request the
   page makes (the site is a custom PHP app, not WordPress) before considering a browser.
5. **Watch disk**: the server had 35 GB free after deleting galaxynovels. Only if it gets tight, remove the
   Minecraft container (CT 101, `hostcraft`) — it is stopped and unused for this project.
6. **The `workflow` scope** is still missing on the `gh` token, so `.github/workflows/harvest.yml` can only
   be edited in the web UI. The rotation design means nothing *needs* to change there; it would only make
   the matrix nicer (one job per site instead of one job per slot).

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
