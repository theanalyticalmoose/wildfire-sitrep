#!/usr/bin/env python3
"""
Wildfire Aware Situation Report generator.

Fully deterministic: downloads the NIFC Incident Management Situation Report PDF,
parses it programmatically (pdfplumber table + text extraction), fetches live NWS
fire-weather alerts and the SPC Day 1 Fire Weather Outlook, and renders a
self-contained HTML report.

No LLM, no browser, no stored secrets. Designed to run on GitHub Actions.

Env vars (all optional):
  OUTPUT_PATH   where to write the HTML (default: wildfire-sitrep.html)
  CONTACT_EMAIL User-Agent contact string for NWS/NOAA requests
  LOCAL_PDF     path to a local PDF (offline testing; skips download + freshness wait)
  RETRY_INTERVAL_SECONDS  poll interval while waiting for today's edition (default 300 = 5 min)
  MAX_WAIT_MINUTES        give up after this long and fail the run (default 60 = 1 h)
"""

import os
import re
import sys
import html
import json
import datetime
import time
import urllib.request

import pdfplumber

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

NIFC_PDF_URL = "https://www.nifc.gov/nicc-files/sitreprt.pdf"
NWS_ALERTS_URL = ("https://api.weather.gov/alerts/active?event="
                  "Red%20Flag%20Warning,Fire%20Weather%20Watch,"
                  "Extremely%20Dangerous%20Situation")
SPC_URL = "https://www.spc.noaa.gov/products/fire_wx/fwdy1.html"

CONTACT = os.environ.get("CONTACT_EMAIL", "wildfire-sitrep@example.com")
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", "wildfire-sitrep.html")
UA = f"wildfire-sitrep {CONTACT}"

GACC_NAMES = {
    "AICC": "Alaska", "NWCC": "Northwest", "ONCC": "Northern California",
    "OSCC": "Southern California", "NRCC": "Northern Rockies",
    "GBCC": "Great Basin", "SWCC": "Southwest", "RMCC": "Rocky Mountain",
    "EACC": "Eastern Area", "SACC": "Southern Area",
}
GACC_CELL_ORDER = ["AICC", "NWCC", "ONCC", "OSCC", "NRCC",
                   "GBCC", "SWCC", "RMCC", "EACC", "SACC"]
SECTION_TITLE_TO_CODE = {
    "Alaska": "AICC", "Northwest": "NWCC", "Northern California": "ONCC",
    "Southern California": "OSCC", "Northern Rockies": "NRCC",
    "Great Basin": "GBCC", "Southwest": "SWCC", "Rocky Mountain": "RMCC",
    "Eastern": "EACC", "Southern": "SACC",
}


# --------------------------------------------------------------------------- #
# Fetch helpers
# --------------------------------------------------------------------------- #
def http_get(url, headers=None, binary=False, retries=3, timeout=30, bust=True):
    """GET with an optional cache-buster, no-cache headers, and simple retries.

    bust=True appends a cache-buster query param -- use for static CDN-cached
    files (the NIFC PDF, SPC HTML) that can otherwise be served stale. Leave it
    False for JSON APIs like api.weather.gov that reject unknown query params.
    """
    full_url = url
    if bust:
        sep = "&" if "?" in url else "?"
        full_url = f"{url}{sep}_cb={int(datetime.datetime.now().timestamp())}"
    hdrs = {"User-Agent": UA, "Cache-Control": "no-cache", "Pragma": "no-cache"}
    if headers:
        hdrs.update(headers)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(full_url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            return data if binary else data.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"  fetch attempt {attempt + 1} failed: {e}", file=sys.stderr)
    raise RuntimeError(f"GET failed after {retries} tries: {url} ({last})")


# --------------------------------------------------------------------------- #
# PDF parsing
# --------------------------------------------------------------------------- #
def collapse(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def parse_pdf(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        full = ""
        for p in pdf.pages:
            t = p.extract_text()
            if t:
                full += t + "\n"
        fire_rows = []
        for p in pdf.pages:
            for tbl in p.extract_tables():
                for row in tbl:
                    if is_fire_row(row):
                        fire_rows.append(row)

    data = {"full_text": full}
    parse_header(full, data)
    parse_gacc_summary(full, data)
    parse_sections(full, data)
    data["fires"] = build_fires(fire_rows, full, data)
    data["weather"] = parse_weather(full)
    return data


def is_fire_row(row):
    """A fire-data row: >= 15 cells, col 1 is a unit code like 'CO-CUX'."""
    if not row or len(row) < 15:
        return False
    name = collapse(row[0])
    unit = collapse(row[1])
    if not name or name.lower() == "incident name":
        return False
    return bool(re.match(r"^[A-Z]{2}-", unit))


def parse_header(full, data):
    m = re.search(r"([A-Z][a-z]+day\s+[A-Z][a-z]+\s+\d{1,2},\s+\d{4})", full)
    data["report_date"] = m.group(1) if m else datetime.date.today().strftime("%A %B %d, %Y")

    m = re.search(r"National Preparedness Level\s+(\d)", full)
    data["national_pl"] = int(m.group(1)) if m else 1

    m = re.search(r"Initial attack activity:\s*([^\n]+)", full)
    data["ia_activity"] = collapse(m.group(1)) if m else "N/A"

    def grab(label):
        mm = re.search(re.escape(label) + r":\s*(\d+)", full)
        return int(mm.group(1)) if mm else 0

    data["national"] = {
        "new_large": grab("New large incidents"),
        "contained": grab("Large fires contained"),
        "uncontained": grab("Uncontained large fires"),
        "cimts": grab("CIMTs committed"),
    }


def parse_gacc_summary(full, data):
    """Parse the Active Incident Resource Summary table (from text rows)."""
    summary = {}
    for code in GACC_NAMES:
        m = re.search(
            rf"^{code}\s+(\d)\s+(\d+)\s+([\d,]+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d,]+)\s+(-?\d+)",
            full, re.MULTILINE)
        if m:
            summary[code] = {
                "pl": int(m.group(1)), "incidents": int(m.group(2)),
                "acres": m.group(3), "crews": int(m.group(4)),
                "engines": int(m.group(5)), "helicopters": int(m.group(6)),
                "personnel": m.group(7),
            }
        else:
            summary[code] = {"pl": 1, "incidents": 0, "acres": "0", "crews": 0,
                             "engines": 0, "helicopters": 0, "personnel": "0"}
    m = re.search(r"^Total\s+-*\s+\d+\s+([\d,]+)", full, re.MULTILINE)
    data["gacc_summary"] = summary
    data["total_acres"] = m.group(1) if m else "0"


def parse_sections(full, data):
    """Find each active GACC narrative section + its PL and activity stats."""
    sections = {}
    pat = re.compile(r"^(.+?) Area \(PL (\d)\)\s*$", re.MULTILINE)
    matches = list(pat.finditer(full))
    for i, m in enumerate(matches):
        code = SECTION_TITLE_TO_CODE.get(m.group(1).strip())
        if not code:
            continue
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full)
        block = full[start:end]

        def g(label):
            mm = re.search(re.escape(label) + r":\s*(\d+)", block)
            return int(mm.group(1)) if mm else 0

        sections[code] = {
            "start": start, "end": end, "pl": int(m.group(2)),
            "new_fires": g("New fires"), "new_large": g("New large incidents"),
            "uncontained": g("Uncontained large fires"),
            "cimts": g("CIMTs Committed"),
        }
    data["sections"] = sections


def build_fires(fire_rows, full, data):
    """Turn raw table rows into structured fire records with GACC + narrative."""
    sec_bounds = sorted(((s["start"], code) for code, s in data["sections"].items()))
    fires = []
    for row in fire_rows:
        cells = [collapse(c) for c in row]
        name_raw = cells[0]
        is_new = name_raw.startswith("*")
        name = name_raw.lstrip("* ").strip()
        unit = cells[1]
        acres = cells[2]
        # Column order: name,unit,acres,chg_acres,pct,"Ctn",est,ppl,chg_ppl,
        #               crews,eng,heli,strc,cost,owner
        fire = {
            "name": name, "unit": unit, "state": unit.split("-")[0],
            "new": is_new, "acres": acres, "chg": cells[3], "pct": cells[4],
            "ppl": cells[7], "strc": cells[12],
            "cost": cells[13] if len(cells) > 13 else cells[-1],
            "gacc": None, "pos": None, "narr": "",
        }
        m = re.search(re.escape(name) + r"[^\n]{0,25}" + re.escape(acres), full)
        if m is None:
            # Fallback (long/wrapped names, complexes): locate the fire by its
            # narrative line instead of its table row.
            m = re.search(narrative_name_re(name), full)
        if m:
            fire["pos"] = m.start()
            for start, code in sec_bounds:
                if start <= m.start():
                    fire["gacc"] = code
                else:
                    break
        fires.append(fire)

    attach_narratives(fires, full, data)
    return fires


def narrative_name_re(name):
    """Match a fire's name at the start of its narrative. Complexes read
    'Name (5 fires),' so allow an optional parenthetical before the comma."""
    return (r"(?:\*\s*)?" + re.escape(name) +
            r"(?:\s*\([^)]{0,30}\))?\s*,")


def attach_narratives(fires, full, data):
    """Attach each fire's narrative paragraph (best-effort; optional)."""
    by_section = {}
    for f in fires:
        by_section.setdefault(f["gacc"], []).append(f)

    for code, sec in data["sections"].items():
        block = full[sec["start"]:sec["end"]]
        mstart = re.search(r"(Uncontained large fires:\s*\d+|CIMTs Committed:\s*\d+)", block)
        mend = re.search(r"(Total\s+Chge|Incident Name)", block)
        narr_block = block[(mstart.end() if mstart else 0):(mend.start() if mend else len(block))]

        hits = []
        for f in by_section.get(code, []):
            m = re.search(narrative_name_re(f["name"]), narr_block)
            if m:
                hits.append((m.start(), f))
        hits.sort()
        for idx, (start, f) in enumerate(hits):
            end = hits[idx + 1][0] if idx + 1 < len(hits) else len(narr_block)
            f["narr"] = re.sub(r"^\*\s*", "", collapse(narr_block[start:end]))


def parse_weather(full):
    m = re.search(r"Predictive Services Discussion:\s*(.+?)\n(?:National Predictive"
                  r" Services Outlook|National Weather Service)", full, re.DOTALL)
    if not m:
        return ""
    paras, cur = [], []
    for line in m.group(1).strip().split("\n"):
        if line.strip() == "":
            if cur:
                paras.append(collapse(" ".join(cur)))
                cur = []
        else:
            cur.append(line.strip())
    if cur:
        paras.append(collapse(" ".join(cur)))
    return "\n\n".join(paras)


# --------------------------------------------------------------------------- #
# NWS + SPC
# --------------------------------------------------------------------------- #
def fetch_nws():
    try:
        feats = json.loads(http_get(NWS_ALERTS_URL, bust=False)).get("features", [])
    except Exception as e:  # noqa: BLE001
        print(f"  NWS fetch failed: {e}", file=sys.stderr)
        return []
    out = []
    for f in feats:
        p = f.get("properties", {})
        desc = p.get("description", "") or ""
        wind = re.search(r"WIND[.\s]*\.\.\.\s*([^\n]*)", desc)
        rh = re.search(r"RELATIVE HUMIDITY[.\s]*\.\.\.\s*([^\n]*)", desc)
        out.append({
            "event": p.get("event", ""), "area": p.get("areaDesc", ""),
            "headline": p.get("headline", ""),
            "onset": (p.get("onset") or "")[:16].replace("T", " "),
            "ends": (p.get("ends") or "")[:16].replace("T", " "),
            "wind": collapse(wind.group(1)) if wind else "",
            "rh": collapse(rh.group(1)) if rh else "",
        })
    return out


def fetch_spc():
    """Return dict with valid time, issuance, no_risk flag, and discussion body."""
    try:
        txt = http_get(SPC_URL)
    except Exception as e:  # noqa: BLE001
        print(f"  SPC fetch failed: {e}", file=sys.stderr)
        return {"available": False}

    valid = re.search(r"Valid\s+(\d{6}Z\s*-\s*\d{6}Z)", txt)
    issued = re.search(r"\d{3,4}\s+[AP]M\s+[A-Z]{2,4}\s+\w{3}\s+\w{3}\s+\d{2}\s+\d{4}", txt)
    no_risk = "No Risk Areas Forecast" in txt

    body = ""
    mb = re.search(r"\.\.\.Synopsis\.\.\.(.+?)(?:\.\.[A-Z][a-z]+\.\.|\n\.\.\.Please see)",
                   txt, re.DOTALL)
    if mb:
        body = collapse(mb.group(1))

    return {"available": True, "no_risk": no_risk,
            "valid": collapse(valid.group(1)) if valid else "",
            "issued": collapse(issued.group(0)) if issued else "",
            "body": body}


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #
# Brand severity ramp: olive, tan, orange, salmon, maroon.
PLC = {1: "#6c733d", 2: "#bb8c4d", 3: "#d58317", 4: "#e87f7f", 5: "#5f0000"}
PLC_FG = {1: "#fff", 2: "#2f292b", 3: "#fff", 4: "#2f292b", 5: "#fff"}


def esc(x):
    return html.escape(str(x))


def pl_badge(pl, small=False):
    fs = "12px" if small else "13px"
    return (f'<span style="background:{PLC.get(pl, "#6c733d")};'
            f'color:{PLC_FG.get(pl, "#fff")};font-weight:700;'
            f'padding:2px 9px;border-radius:12px;font-size:{fs};white-space:nowrap;">PL {pl}</span>')


def new_badge():
    return ('<span style="color:#280069;background:rgba(40,0,105,0.07);'
            'border:1px solid rgba(40,0,105,0.30);font-weight:700;padding:1px 8px;'
            'border-radius:10px;font-size:11px;letter-spacing:0.5px;">NEW</span>')


def contained_badge():
    return ('<span style="color:#6c733d;background:rgba(108,115,61,0.14);font-weight:700;'
            'padding:1px 8px;border-radius:10px;font-size:11px;">CONTAINED</span>')


def state_tag(s):
    return (f'<span style="background:#eae4d8;color:#5c5244;padding:1px 7px;border-radius:6px;'
            f'font-size:12px;font-weight:600;">{esc(s)}</span>')


def acnum(v):
    try:
        return int(str(v).replace(",", ""))
    except ValueError:
        return 0


def chg_html(chg):
    if chg in ("---", "0", "", None):
        return ""
    if str(chg).startswith("-"):
        return f' <span style="color:#8a8178;">({esc(chg)})</span>'
    return f' <span style="color:#d58317;">(+{esc(chg)})</span>'


def stat_row(f):
    parts = [f'<b style="color:#d58317;">{esc(f["acres"])}</b> acres{chg_html(f["chg"])}',
             f'{esc(f["pct"])}% contained', f'{esc(f["ppl"])} personnel']
    if f["strc"] and str(f["strc"]) not in ("0", "---", ""):
        parts.append(f'<span style="color:#5f0000;">{esc(f["strc"])} structures lost</span>')
    parts.append(f'{esc(f["cost"])} to date')
    return " &nbsp;·&nbsp; ".join(parts)


def render(data, nws, spc):
    d = data
    fires = d["fires"]
    contained = [f for f in fires if f["pct"] == "100"]
    active = [f for f in fires if f["pct"] != "100"]
    new_active = [f for f in active if f["new"]]
    new_states = []
    for f in new_active:
        if f["state"] not in new_states:
            new_states.append(f["state"])
    new_contained_ct = sum(1 for f in contained if f["new"])

    H = []
    H.append('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width, initial-scale=1">')
    H.append(f'<title>Wildfire Aware Situation Report — {esc(d["report_date"])}</title>')
    H.append(STYLE)
    H.append('</head><body><div class="wrap">')

    # GACC areas that will get a section below (drives grid-cell links)
    linked_codes = {c for c in d["sections"]
                    if any(f["gacc"] == c and f["pct"] != "100" for f in fires)}

    H.append(f'<div class="header">'
             f'<div class="muted">{esc(d["report_date"])} · 0730 MDT · '
             f'National Interagency Fire Center</div></div>')

    # PLB markers let the email step rebuild this banner as a table so its
    # maroon fill survives email clients that drop div backgrounds.
    H.append('<!--PLB-->')
    H.append(f'<div class="plbanner"><div class="big">National Preparedness Level '
             f'{d["national_pl"]}</div><div style="margin-top:8px;opacity:0.95;">'
             f'Initial attack activity: {esc(d["ia_activity"])}. '
             f'{d["national"]["new_large"]} new large incidents reported nationally, with '
             f'{d["national"]["uncontained"]} uncontained large fires currently active '
             f'across the country.</div></div>')
    H.append('<!--/PLB-->')

    cards = [("Uncontained Large Fires", d["national"]["uncontained"], "#large-fires"),
             ("New Large Fires", d["national"]["new_large"], "#new-fires"),
             ("Large Fires Contained", d["national"]["contained"], "#contained-fires"),
             ("CIMTs Committed", d["national"]["cimts"], "#large-fires"),
             ("Total Active Acres", d["total_acres"], "#gacc-levels")]
    H.append('<div class="stats">')
    for label, n, href in cards:
        H.append(f'<a class="stat-a" href="{href}"><div class="stat">'
                 f'<div class="n">{esc(n)}</div>'
                 f'<div class="l">{esc(label)}</div></div></a>')
    H.append('</div>')

    H.append('<h2 id="gacc-levels">GACC Preparedness Levels</h2><div class="gaccgrid">')
    for code in GACC_CELL_ORDER:
        pl = d["gacc_summary"].get(code, {}).get("pl", 1)
        cell = (f'<div class="gcell"><div><div class="code">{code}</div>'
                f'<div class="full">{esc(GACC_NAMES[code])}</div></div>{pl_badge(pl)}</div>')
        if code in linked_codes:
            cell = (f'<a class="gcell-a" href="#gacc-{code}" '
                    f'title="Jump to {code} large fires">{cell}</a>')
        H.append(cell)
    H.append('</div>')

    # Fire weather at a glance: SPC verdict + alert counts, linking down
    rfw_ct = sum(1 for a in nws if a["event"] == "Red Flag Warning")
    fww_ct = sum(1 for a in nws if a["event"] == "Fire Weather Watch")
    other_ct = len(nws) - rfw_ct - fww_ct
    H.append('<h2 id="wx-glance">Fire Weather at a Glance</h2><div class="wxcards">')
    if not spc.get("available"):
        H.append('<a class="stat-a" href="#spc"><div class="stat"><div class="n">N/A</div>'
                 '<div class="l">SPC Day 1 Outlook</div></div></a>')
    elif spc.get("no_risk"):
        H.append('<a class="stat-a" href="#spc"><div class="stat wx-ok">'
                 '<div class="n">NO RISK</div><div class="l">SPC Day 1 Outlook</div></div></a>')
    else:
        H.append('<a class="stat-a" href="#spc"><div class="stat wx-rfw">'
                 '<div class="n">RISK</div><div class="l">SPC Day 1 Outlook</div></div></a>')
    H.append(f'<a class="stat-a" href="#nws"><div class="stat {"wx-rfw" if rfw_ct else "wx-ok"}">'
             f'<div class="n">{rfw_ct}</div><div class="l">Red Flag Warnings</div></div></a>')
    H.append(f'<a class="stat-a" href="#nws"><div class="stat {"wx-fww" if fww_ct else "wx-ok"}">'
             f'<div class="n">{fww_ct}</div><div class="l">Fire Weather Watches</div></div></a>')
    if other_ct:
        H.append(f'<a class="stat-a" href="#nws"><div class="stat wx-rfw">'
                 f'<div class="n">{other_ct}</div><div class="l">Other Fire Alerts</div></div></a>')
    H.append('</div>')

    extra = (f' (plus {new_contained_ct} reported new, already contained)'
             if new_contained_ct else '')
    H.append(f'<h2 id="new-fires">New Large Fires <span class="sub">— {len(new_active)} '
             f'active new incidents in {", ".join(new_states) or "—"}{extra}</span></h2>')
    for f in sorted(new_active, key=lambda x: acnum(x["acres"]), reverse=True):
        H.append(f'<div class="fire new"><div class="fname">{esc(f["name"])} '
                 f'{state_tag(f["state"])} {new_badge()}</div>')
        if f["narr"]:
            H.append(f'<div class="narr">{esc(f["narr"])}</div>')
        H.append(f'<div class="srow"><b style="color:#d58317;">{esc(f["acres"])}</b> acres '
                 f'&nbsp;·&nbsp; {esc(f["pct"])}% contained &nbsp;·&nbsp; '
                 f'{esc(f["gacc"] or "")}</div></div>')

    H.append('<h2 id="contained-fires">Contained Fires</h2>')
    if not contained:
        H.append('<div class="banner-ok">No large fires reached 100% containment this report.</div>')
    for f in sorted(contained, key=lambda x: acnum(x["acres"]), reverse=True):
        nb = " " + new_badge() if f["new"] else ""
        H.append(f'<div class="fire contained-card"><div class="fname">{esc(f["name"])} '
                 f'{state_tag(f["state"])}{nb} {contained_badge()}</div>'
                 f'<div class="srow" style="border-top:none;padding-top:0;">'
                 f'<b style="color:#d58317;">{esc(f["acres"])}</b> acres &nbsp;·&nbsp; '
                 f'100% contained &nbsp;·&nbsp; {esc(f["gacc"] or "")} &nbsp;·&nbsp; '
                 f'{esc(f["cost"])} to date</div></div>')

    H.append('<h2 id="weather">Predictive Services Discussion</h2><div class="card wxtext">')
    for para in (d["weather"] or "N/A").split("\n\n"):
        H.append(f'<p>{esc(para)}</p>')
    H.append('</div>')

    H.append('<h2 id="spc">SPC Day 1 Fire Weather Outlook</h2>')
    if not spc.get("available"):
        H.append('<div class="alert spc-elevated">Live SPC product was unavailable at '
                 'generation time.</div>')
    elif spc.get("no_risk"):
        vt = f' (valid {esc(spc["valid"])}' + (f', issued {esc(spc["issued"])}'
                                               if spc.get("issued") else "") + ')'
        H.append(f'<div class="banner-ok"><b>No Risk Areas Forecast.</b> The SPC Day 1 Fire '
                 f'Weather Outlook{vt} delineates no Critical or Elevated risk areas. '
                 f'Pulled live from spc.noaa.gov.</div>')
        if spc.get("body"):
            H.append('<div class="muted" style="margin:6px 0 10px;font-size:14px;">'
                     'Forecaster discussion (localized concerns kept below formal highlight '
                     'criteria):</div>')
            H.append(f'<div class="alert spc-elevated" style="font-size:14px;">'
                     f'{esc(spc["body"])}</div>')
    else:
        vt = f' (valid {esc(spc["valid"])})' if spc.get("valid") else ""
        H.append(f'<div class="alert spc-critical"><b>Risk areas forecast{vt}.</b> '
                 f'See discussion below.</div>')
        if spc.get("body"):
            H.append(f'<div class="alert spc-elevated" style="font-size:14px;">'
                     f'{esc(spc["body"])}</div>')

    H.append('<h2 id="nws">NWS Fire Weather Alerts</h2>')
    if not nws:
        H.append('<div class="banner-ok">No active Red Flag Warnings or Fire Weather Watches.</div>')
    for a in nws:
        cls = "rfw" if a["event"] == "Red Flag Warning" else "fww"
        H.append(f'<div class="alert {cls}"><div class="atitle">{esc(a["event"])} — '
                 f'{esc(a["area"])}</div>')
        if a["headline"]:
            H.append(f'<div style="font-size:14px;margin-bottom:5px;">{esc(a["headline"])}</div>')
        det = []
        if a["wind"]:
            det.append(f'<b>Winds:</b> {esc(a["wind"])}')
        if a["rh"]:
            det.append(f'<b>Min RH:</b> {esc(a["rh"])}')
        if a["onset"] or a["ends"]:
            det.append(f'<b>Valid:</b> {esc(a["onset"])} to {esc(a["ends"])}')
        H.append(f'<div style="font-size:13px;">{" &nbsp;·&nbsp; ".join(det)}</div></div>')

    H.append('<h2 id="large-fires">Large Fires <span class="sub">— Grouped by GACC</span></h2>')
    active_codes = sorted(linked_codes,
                          key=lambda c: (-d["sections"][c]["pl"],
                                         -acnum(d["gacc_summary"].get(c, {}).get("acres", "0"))))
    for code in active_codes:
        sec = d["sections"][code]
        gfires = [f for f in fires if f["gacc"] == code and f["pct"] != "100"]
        if not gfires:
            continue
        border = "#5f0000" if sec["pl"] >= 4 else "#d58317"
        summ = d["gacc_summary"].get(code, {})
        H.append(f'<div class="gacchead" id="gacc-{code}" style="border-left:4px solid {border};">'
                 f'<div class="top">{esc(GACC_NAMES[code])} Area '
                 f'<span class="muted" style="font-weight:400;">({code})</span> '
                 f'{pl_badge(sec["pl"], True)}</div>')
        meta = (f'{summ.get("incidents", 0)} total incidents · '
                f'{esc(summ.get("acres", "0"))} cumulative acres')
        if sec["cimts"]:
            meta += f' · {sec["cimts"]} CIMTs committed'
        H.append(f'<div class="meta">{meta}</div>'
                 f'<div class="mrow"><span>New fires: <b>{sec["new_fires"]}</b></span>'
                 f'<span>New large: <b>{sec["new_large"]}</b></span>'
                 f'<span>Uncontained: <b>{sec["uncontained"]}</b></span></div></div>')
        for f in sorted(gfires, key=lambda x: acnum(x["acres"]), reverse=True):
            ncls = " new" if f["new"] else ""
            nb = " " + new_badge() if f["new"] else ""
            H.append(f'<div class="fire{ncls}"><div class="fname">{esc(f["name"])} '
                     f'{state_tag(f["state"])}{nb}</div>')
            if f["narr"]:
                H.append(f'<div class="narr">{esc(f["narr"])}</div>')
            H.append(f'<div class="srow">{stat_row(f)}</div></div>')

    gen = datetime.datetime.now().strftime("%B %d, %Y %H:%M")
    H.append(f'<div class="footer"><b>Wildfire Aware Situation Report</b> · Generated {gen}<br>'
             'Sources: '
             '<a href="https://www.nifc.gov/nicc/predictive-services/intelligence">'
             'NIFC Incident Management Sit Report</a>'
             '<a href="https://www.weather.gov/fire/">NWS Fire Weather</a>'
             '<a href="https://www.spc.noaa.gov/products/fire_wx/fwdy1.html">'
             'SPC Fire Weather Outlook</a></div>')
    H.append('</div>')
    H.append(SCROLL_SCRIPT)
    H.append('</body></html>')
    return "\n".join(H)


SCROLL_SCRIPT = """<script>
document.addEventListener('click', function (e) {
  var a = e.target.closest('a[href^="#"]');
  if (!a) return;
  var el = document.getElementById(a.getAttribute('href').slice(1));
  if (el) { e.preventDefault(); el.scrollIntoView({behavior: 'smooth', block: 'start'}); }
});
</script>"""


STYLE = """<style>
@import url('https://fonts.googleapis.com/css2?family=Oswald:wght@500;600;700&family=David+Libre:wght@400;500;700&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
body{background:#f2f2f2;color:#2f292b;font-family:"David Libre",Georgia,"Times New Roman",serif;font-size:15.5px;line-height:1.6;padding:20px 14px 60px}
.wrap{max-width:860px;margin:0 auto}
.muted{color:#8a8178}
a{color:#074259}
h1,h2,.stat .n,.plbanner .big,.gcell .code{font-family:"Oswald","Arial Narrow",sans-serif}
.header{border-left:5px solid #5f0000;padding:6px 0 6px 16px;margin-bottom:22px}
.header h1{font-size:30px;font-weight:700;letter-spacing:0.3px}
.plbanner{background:#5f0000;background:linear-gradient(135deg,#5f0000,#93400c);border-radius:14px;padding:20px 22px;margin-bottom:22px;color:#f8f4ec}
.plbanner .big{font-size:34px;font-weight:700;line-height:1.15;letter-spacing:0.5px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:30px}
.stat{background:#fff;border:1px solid #ddd5c7;border-radius:12px;padding:16px}
.stat .n{font-size:30px;font-weight:600;color:#5f0000}
.stat .l{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#8a8178;margin-top:4px;font-family:"Oswald",sans-serif;font-weight:500}
.stat-a{text-decoration:none;color:#2f292b;display:block}
.stat-a .stat{transition:border-color 0.15s, box-shadow 0.15s;height:100%}
.stat-a:hover .stat{border-color:#5f0000;box-shadow:0 1px 6px rgba(95,0,0,0.18)}
h2{font-size:21px;font-weight:600;letter-spacing:0.4px;margin:34px 0 14px;padding-bottom:8px;border-bottom:2px solid #5f0000;color:#2f292b;scroll-margin-top:12px}
h2 .sub{font-size:13px;font-weight:400;color:#8a8178;font-family:"David Libre",serif;letter-spacing:0}
.wxcards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:14px}
.wx-ok{background:rgba(108,115,61,0.10);border-color:#6c733d}
.wx-ok .n{color:#535c2b}
.wx-rfw{background:rgba(95,0,0,0.06);border-color:#5f0000}
.wx-rfw .n{color:#5f0000}
.wx-fww{background:rgba(187,140,77,0.12);border-color:#bb8c4d}
.wx-fww .n{color:#8a5f22}
.gcell-a{text-decoration:none;color:#2f292b;display:block}
.gcell-a .gcell{transition:border-color 0.15s, box-shadow 0.15s}
.gcell-a:hover .gcell{border-color:#5f0000;box-shadow:0 1px 6px rgba(95,0,0,0.18)}
.gaccgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.gcell{background:#fff;border:1px solid #ddd5c7;border-radius:10px;padding:12px;display:flex;justify-content:space-between;align-items:center;gap:8px}
.gcell .code{font-weight:600;font-size:16px;letter-spacing:0.5px}
.gcell .full{font-size:11px;color:#8a8178}
.card{background:#fff;border:1px solid #ddd5c7;border-radius:12px;padding:16px 18px;margin-bottom:14px}
.fire{background:#fff;border:1px solid #ddd5c7;border-radius:10px;padding:14px 16px;margin-bottom:12px}
.fire.new{border-left:4px solid #280069}
.fire .fname{font-size:17px;font-weight:700;display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:6px}
.fire .narr{color:#4f4944;font-size:15px;margin-bottom:9px}
.fire .srow{font-size:14px;color:#4a443f;border-top:1px solid #e8e1d4;padding-top:8px}
.contained-card{border-left:4px solid #6c733d}
.gacchead{border-radius:12px;padding:14px 18px;margin:22px 0 12px;background:#2f292b;border:1px solid #2f292b;color:#f2f2f2;scroll-margin-top:12px}
.gacchead .muted{color:#b8b0a4}
.gacchead .top{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:22px;font-weight:700}
.gacchead .meta{color:#b8b0a4;font-size:15px;margin-top:4px}
.gacchead .mrow{display:flex;gap:20px;flex-wrap:wrap;margin-top:8px;font-size:16px}
.gacchead .mrow b{color:#e59a3c}
.alert{border-radius:10px;padding:13px 16px;margin-bottom:11px;background:#fff}
.spc-critical,.rfw{background:rgba(95,0,0,0.05);border:1px solid #5f0000;border-left:5px solid #5f0000}
.spc-elevated,.fww{background:rgba(187,140,77,0.10);border:1px solid #bb8c4d;border-left:5px solid #bb8c4d}
.alert .atitle{font-weight:700;margin-bottom:5px}
.banner-ok{background:rgba(108,115,61,0.10);border:1px solid #6c733d;border-left:5px solid #6c733d;border-radius:10px;padding:14px 16px;margin-bottom:12px}
.wxtext p{margin-bottom:12px}
.footer{margin-top:44px;border-top:2px solid #5f0000;padding-top:18px;color:#8a8178;font-size:13px}
.footer a{margin-right:14px}
</style>"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def target_report_date():
    """Today's date in US Mountain time (the report is stamped 0730 MDT)."""
    if ZoneInfo is not None:
        try:
            return datetime.datetime.now(ZoneInfo("America/Denver")).date()
        except Exception:  # pragma: no cover
            pass
    # Fallback if tzdata is unavailable: MDT = UTC-6.
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=6)).date()


def report_date_to_date(s):
    """Parse the PDF's 'Thursday July 9, 2026' header into a date object."""
    s = (s or "").strip()
    try:
        return datetime.datetime.strptime(s, "%A %B %d, %Y").date()
    except ValueError:
        pass
    m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", s)
    if m:
        try:
            return datetime.datetime.strptime(m.group(1), "%B %d, %Y").date()
        except ValueError:
            pass
    return None


def get_fresh_pdf():
    """Download the NIFC PDF and keep retrying every RETRY_INTERVAL_SECONDS
    until the report is dated for today (Mountain time), or MAX_WAIT_MINUTES
    elapses. Returns parsed data.

    If today's edition never appears within the window, raises SystemExit so
    the workflow fails (triggering the failure-email alert) and the site keeps
    yesterday's report rather than republishing stale data.
    """
    pdf_path = "/tmp/sitreprt.pdf"
    target = target_report_date()
    interval = int(os.environ.get("RETRY_INTERVAL_SECONDS", "300"))
    max_wait = int(os.environ.get("MAX_WAIT_MINUTES", "60"))
    deadline = time.monotonic() + max_wait * 60
    attempt = 0
    while True:
        attempt += 1
        print(f"Downloading NIFC PDF (attempt {attempt}, target {target})...")
        with open(pdf_path, "wb") as fh:
            fh.write(http_get(NIFC_PDF_URL, binary=True))
        data = parse_pdf(pdf_path)
        rd = report_date_to_date(data["report_date"])
        if rd is None:
            print(f"  Could not parse report date '{data['report_date']}'; "
                  f"proceeding with the downloaded edition.")
            return data
        if rd >= target:
            print(f"  Fresh edition: dated {rd} (target {target}).")
            return data
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SystemExit(
                f"NIFC report still stale (dated {rd}, expected {target}) after "
                f"{max_wait} min. Failing so the alert fires; the published site "
                f"keeps yesterday's report.")
        wait = min(interval, remaining)
        print(f"  Stale edition dated {rd} < target {target}. "
              f"Waiting {int(wait)}s, then retrying...")
        time.sleep(wait)


def main():
    local_pdf = os.environ.get("LOCAL_PDF")
    if local_pdf and os.path.exists(local_pdf):
        print(f"Using local PDF (offline mode, freshness check skipped): {local_pdf}")
        data = parse_pdf(local_pdf)
    else:
        data = get_fresh_pdf()
    print(f"Report date: {data['report_date']} | National PL {data['national_pl']}")

    print("Fetching NWS alerts...")
    nws = fetch_nws()
    print(f"  {len(nws)} active alert(s)")
    print("Fetching SPC Day 1 outlook...")
    spc = fetch_spc()
    print(f"  SPC available={spc.get('available')} no_risk={spc.get('no_risk')}")

    html_out = render(data, nws, spc)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(html_out)
    print(f"Wrote {len(html_out)} bytes -> {OUTPUT_PATH}")
    print(f"Parsed {len(data['fires'])} fires "
          f"({sum(1 for f in data['fires'] if f['new'])} new, "
          f"{sum(1 for f in data['fires'] if f['pct']=='100')} contained)")


if __name__ == "__main__":
    main()
