# Wildfire Aware Situation Report — cloud edition

Generates a daily HTML wildfire situation report from public NIFC / NWS / SPC
data and publishes it to GitHub Pages. Runs entirely on GitHub Actions — no
local machine, no LLM, no browser, and no stored access tokens.

**Live report:**
<https://theanalyticalmoose.github.io/wildfire-sitrep/wildfire-sitrep.html>

## What it does

`generate.py`:

1. Downloads the [NIFC Incident Management Situation Report](https://www.nifc.gov/nicc/predictive-services/intelligence)
   PDF and parses it **programmatically** with `pdfplumber` (table extraction
   for the fire-data tables, text extraction for narratives, preparedness
   levels, and the Predictive Services weather discussion). Nothing is
   hard-coded — it regenerates from whatever the PDF says each day.
2. Fetches active NWS fire-weather alerts (Red Flag Warnings, Fire Weather
   Watches, and Extremely Dangerous Situation alerts) from `api.weather.gov`.
3. Fetches the live SPC Day 1 Fire Weather Outlook from `spc.noaa.gov`
   (plain HTTPS with a cache-buster + `no-cache` header — no browser needed).
4. Renders a single self-contained `wildfire-sitrep.html` with national stats,
   GACC preparedness levels, new/contained/large fires grouped by GACC, the
   weather discussion, the SPC outlook, and active NWS alerts.

The GitHub Actions workflow (`.github/workflows/daily-sitrep.yml`) runs it
daily and commits the result **only if the HTML changed**, so you don't get
empty daily commits. The NWS and SPC fetches are best-effort — if one fails,
the report still renders without it; only a stale NIFC PDF fails the run
(see below). If a `RESEND_API_KEY` secret is configured, the workflow also
emails each day's report (see "Email delivery" below).

## Repository contents

- `generate.py` — the whole generator: fetch, parse, render.
- `requirements.txt` — one dependency, `pdfplumber`.
- `.github/workflows/daily-sitrep.yml` — daily schedule + commit-if-changed.
- `wildfire-sitrep.html` — the latest generated report (committed by the
  workflow, served by GitHub Pages).

## One-time setup (for a fork)

1. Fork or copy the repo with the files above in place.
2. Enable GitHub Pages: **Settings → Pages → Build and deployment → Deploy
   from a branch → `main` / root**.
3. The workflow declares `permissions: contents: write` and commits with the
   built-in `GITHUB_TOKEN`, so there is **no personal access token to create
   or store**. If your org restricts workflow permissions, allow read/write
   under **Settings → Actions → General → Workflow permissions**.
4. Optionally set `CONTACT_EMAIL` in the workflow's `Generate report` step —
   it's sent as the User-Agent contact for NWS/NOAA requests, per their API
   etiquette.

Your report will then be live at
`https://<user>.github.io/<repo>/wildfire-sitrep.html`.

## Schedule

Runs daily at **12:45 UTC** (~06:45 MDT), ahead of NIFC's typical posting time
of ~07:30 MDT; the freshness wait below bridges the gap until the day's
edition appears. Adjust the `cron:` line in the workflow for a different time.
You can also trigger a run manually from the **Actions** tab
(`workflow_dispatch`). If a run fails, GitHub emails the repo owner by
default.

## Waiting for the day's report

NIFC's posting time drifts, so the run may fire before today's PDF is up. The
generator handles this: it checks the PDF's report date against today
(US Mountain time) and, if it's still yesterday's edition, waits and
re-downloads until the current one appears. Two env vars tune this (set them
in the workflow's `Generate report` step to change the defaults):

- `RETRY_INTERVAL_SECONDS` — poll interval while waiting (default `300`, i.e. 5 min)
- `MAX_WAIT_MINUTES` — give up after this long (default `60`, i.e. 1 h)

If today's edition never appears within `MAX_WAIT_MINUTES`, the run exits with
an error (so the failure email fires) and the published site keeps the
previous day's report rather than republishing stale data.

**Off-season note:** NIFC only publishes the situation report daily during
fire season; in the off-season it comes out less often (or not at all). On
days with no new edition the run will fail after `MAX_WAIT_MINUTES` and email
you. If that gets noisy, disable the workflow for the winter (**Actions →
Daily Wildfire Sitrep → ⋯ → Disable workflow**) or stretch the cron to weekly,
and re-enable it in spring.

## Email delivery (optional)

After each day's report is committed, the workflow emails the full HTML
report via [Resend](https://resend.com). To enable it:

1. Create an API key in the Resend dashboard (**API Keys → Create API key**).
2. Add it to this repo as a secret named `RESEND_API_KEY`:
   **Settings → Secrets and variables → Actions → New repository secret**.

With the secret absent, the email step just logs a note and succeeds, so the
workflow works fine without it — this is the only secret the repo uses.

The sender defaults to Resend's built-in `onboarding@resend.dev`, which can
only deliver to the email address that owns the Resend account. Once you
verify a domain in Resend, change `EMAIL_FROM` (and `EMAIL_TO` if needed) in
the workflow's `Email report via Resend` step.

## Running locally

```bash
pip install -r requirements.txt
python generate.py            # fetches everything live, writes wildfire-sitrep.html
```

Optional env vars: `OUTPUT_PATH` (output file path), `CONTACT_EMAIL`
(User-Agent contact), and `LOCAL_PDF` (path to a saved sitrep PDF for offline
testing — skips the download and the freshness wait).
