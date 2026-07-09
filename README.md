# Wildfire Aware Situation Report — cloud edition

Generates a daily HTML wildfire situation report from public NIFC / NWS / SPC
data and publishes it to GitHub Pages. Runs entirely on GitHub Actions — no
local machine, no LLM, no browser, and no stored access tokens.

## What it does

`generate.py`:

1. Downloads the NIFC Incident Management Situation Report PDF and parses it
   **programmatically** with `pdfplumber` (table extraction for the fire-data
   tables, text extraction for narratives, PL levels, and the weather
   discussion). Nothing is hard-coded — it regenerates from whatever the PDF
   says each day.
2. Fetches active NWS Red Flag Warnings / Fire Weather Watches from
   `api.weather.gov`.
3. Fetches the live SPC Day 1 Fire Weather Outlook from `spc.noaa.gov`
   (plain HTTPS with a cache-buster + `no-cache` header — no browser needed).
4. Renders a single self-contained `wildfire-sitrep.html`.

The GitHub Actions workflow runs it daily and commits the result **only if the
HTML changed**, so you don't get empty daily commits.

## One-time setup

1. Put these files in the repo root:
   - `generate.py`
   - `requirements.txt`
   - `.github/workflows/daily-sitrep.yml`  (move `daily-sitrep.yml` into that path)
2. Enable GitHub Pages: **Settings → Pages → Build and deployment → Deploy from
   a branch → `main` / root**.
3. Confirm Actions can write to the repo: **Settings → Actions → General →
   Workflow permissions → Read and write permissions**.
4. That's it. The workflow commits with the built-in `GITHUB_TOKEN`; there is
   **no personal access token to create or store**.

Your report will be live at:
`https://<user>.github.io/<repo>/wildfire-sitrep.html`

## Security note

The original Cowork task file contained a hard-coded GitHub personal access
token in plaintext. **Revoke that token** (GitHub → Settings → Developer
settings → Personal access tokens) — it should be treated as compromised. This
cloud version does not use a PAT at all.

## Schedule

Runs at **13:45 UTC** daily (~07:45 MDT), shortly after NIFC posts the report
around 0730 MDT. Adjust the `cron:` line in the workflow if you want a
different time. You can also trigger a run manually from the **Actions** tab
(`workflow_dispatch`). If a run fails, GitHub emails the repo owner by default.

## Waiting for the day's report

NIFC's posting time drifts, so the run may fire before today's PDF is up. The
generator handles this: it checks the PDF's "Report date" against today
(US Mountain time) and, if it's still yesterday's edition, waits and re-downloads
until the current one appears. Two env vars tune this (set them in the workflow's
`Generate report` step if you want to change the defaults):

- `RETRY_INTERVAL_SECONDS` — poll interval while waiting (default `300`, i.e. 5 min)
- `MAX_WAIT_MINUTES` — give up after this long (default `180`, i.e. 3 h)

If today's edition never appears within `MAX_WAIT_MINUTES`, the run exits with an
error (so the failure email fires) and the published site keeps the previous
day's report rather than republishing stale data. Note: during the off-season, if
NIFC skips a 