# novel-harvest

Harvests web novels (Arabic + English) into Google Drive, running on GitHub Actions because the home
server sits behind an unreliable connection.

## How it works

Each job gets 6 hours max, so progress lives in **Drive**, not on the runner:

- `novels/state/<site>.json` — which novels are finished and where the current one stopped
- `novels/<lang>/<site>/<novel>/NNNN.txt` — one file per chapter, plus `index.json`
- `novels/catalogue/<site>.json` — the site's novel list, built once

A run reads the state, harvests until its budget is nearly gone, and pushes chapters **every ~4 minutes**,
so even a killed job keeps what it wrote. Resume is by file existence: nothing is fetched twice.

## Sites

| site | language | method |
|---|---|---|
| galaxynovels | ar | plain HTTP, `<article>` |
| fanmtl | en | plain HTTP, `.chapter-content`, chapters at `/novel/<slug>_<n>.html` |
| wuxiaworldsite | en | plain HTTP, `.entry-content` |

Sites that need a real browser (novltime, wuxiadreams) or block datacentre IPs (novelfire, novelcool) are
not part of this workflow; they run on the home server when it is online.

## Manual run

Actions → harvest → Run workflow → pick a site and a budget in minutes.

## Secret

`RCLONE_CONF` — the rclone config with the Drive token. Set once in repository settings.