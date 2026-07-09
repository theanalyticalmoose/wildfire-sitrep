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

Runs at **14:15 UTC** daily (~08:15 MDT), shortly after NIFC posts the report
around 0730 MDT. Adjust the `cron:` line in the workflow if you want a
different time. You can also trigger a run manually from the **Actions** tab
(`workflow_dispatch`). If a run fails, GitHub emails the repo owner by default.

## Run it locally

```bash
pip install -r requirements.txt

# Live pull:
OUTPUT_PATH=wildfire-sitrep.html \
CONTACT_EMAIL=you@example.com \
python generate.py

# Offline test against a already-downloaded PDF:
LOCAL_PDF=/path/to/sitreprt.pdf python generate.py
```

## Maintenance notes

- The parser keys off the NIFC report's current layout (GACC summary table,
  `"<Area> Area (PL n)"` section headers, and the 15-column fire tables). If
  NIFC changes the PDF format, the table/section regexes in `generate.py` are
  where you'd adjust.
- Per-fire narratives are best-effort: if one can't be matched it's simply
  omitted, and the numeric data (from the tables) is unaffected.
- To also keep a dated archive, add a copy step in the workflow, e.g.
  `cp wildfire-sitrep.html archive/$(date -u +%F).html` before the commit.
