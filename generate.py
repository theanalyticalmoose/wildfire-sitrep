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
# Short display names so grid cards keep their shape.
GACC_SHORT = dict(GACC_NAMES, ONCC="N. California", OSCC="S. California")
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


def reflow(text):
    """Unwrap hard line-wraps within paragraphs (NWS/SPC products wrap at
    ~68 chars mid-sentence); keep true blank-line paragraph breaks."""
    paras = re.split(r"\n\s*\n", text or "")
    return "\n\n".join(" ".join(l.strip() for l in p.split("\n"))
                       for p in paras if p.strip())


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
    parse_ytd(full, data)
    parse_gacc_summary(full, data)
    parse_sections(full, data)
    # The section headers ("Northwest Area (PL 5)") are authoritative for PL;
    # keep the grid cards consistent even if a summary row fails to parse.
    for code, sec in data["sections"].items():
        if code in data["gacc_summary"]:
            data["gacc_summary"][code]["pl"] = sec["pl"]
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


def parse_ytd(full, data):
    """Year-to-date national totals (last number on the TOTAL FIRES/ACRES
    rows) and NIFC's own % of the ten-year average."""
    ytd = {}
    for key, label in (("fires", "TOTAL FIRES"), ("acres", "TOTAL ACRES")):
        # The daily-activity table uses the same TOTAL FIRES/ACRES labels and
        # appears first; the year-to-date table is the LAST occurrence.
        ms = list(re.finditer(label + r":?\s*([^\n]+)", full))
        if ms:
            nums = re.findall(r"[\d,]+", ms[-1].group(1))
            if nums:
                ytd[key] = nums[-1]
    for key, label in (("fires_pct", "Fires"), ("acres_pct", "Acres")):
        m = re.search(label + r"\s*\(20\d\d\s*[-–—]\s*20\d\d as of today\)"
                      r"\s*[\d,]+\s*(\d+)\s*%", full)
        if m:
            ytd[key] = int(m.group(1))
    data["ytd"] = ytd if "fires" in ytd and "acres" in ytd else None


def parse_gacc_summary(full, data):
    """Parse the Active Incident Resource Summary table (from text rows).

    Every numeric column may carry thousands separators once a GACC is busy
    enough (e.g. NWCC at PL 5 with 1,120 engines), so allow commas in all of
    them and strip when converting."""
    summary = {}
    num = r"([\d,]+)"
    for code in GACC_NAMES:
        m = re.search(
            rf"^{code}\s+(\d)\s+{num}\s+{num}\s+{num}\s+{num}\s+{num}\s+{num}\s+(-?[\d,]+)",
            full, re.MULTILINE)
        if m:
            summary[code] = {
                "pl": int(m.group(1)), "incidents": acnum(m.group(2)),
                "acres": m.group(3), "crews": acnum(m.group(4)),
                "engines": acnum(m.group(5)), "helicopters": acnum(m.group(6)),
                "personnel": m.group(7),
            }
        else:
            summary[code] = {"pl": 1, "incidents": 0, "acres": "0", "crews": 0,
                             "engines": 0, "helicopters": 0, "personnel": "0"}
    m = re.search(r"^Total\s+-*\s+[\d,]+\s+([\d,]+)", full, re.MULTILINE)
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
            m = find_narr_entry(name, full)
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


# What follows a fire's name in a true narrative entry: its unit and agency,
# e.g. "Burns District, BLM." / "Malheur NF, USFS." / "Medford Unit, Oregon
# DOF." Incidental mentions inside other narratives ("...managing the Bald
# Mountain, Second Flat and Jackass Butte incidents") don't have this shape.
NARR_UNIT_RE = re.compile(
    r"\s*[A-Z][^.\n]{0,60}?"
    r"(?:BLM|USFS|USFWS|FWS|NPS|BIA|DNR|DOF|OSFM|Cal\s?Fire|CAL\s?FIRE|"
    r"NF|NWR|District|Unit|Agency|Region|Forest|Park|County|Tribe|Nation)\b"
    r"[^.\n]{0,30}\.")


def find_narr_entry(name, text, require_entry=False):
    """Best match for a fire's narrative entry in text. A fire's name can
    also appear inside ANOTHER fire's narrative (cross-references like
    '...is also managing the Bald Mountain, Second Flat and Jackass Butte
    incidents'), and taking the first occurrence merges every fire in
    between under one card. Score each occurrence instead: +3 if followed
    by unit-and-agency text, +1 at line start, +1 more with a '*' bullet;
    highest score wins, earliest position breaks ties. With require_entry,
    return None unless some occurrence scores > 0 (i.e. looks like a real
    entry, not just a passing mention)."""
    best, best_score = None, -1
    for m in re.finditer(narrative_name_re(name), text):
        score = 0
        if NARR_UNIT_RE.match(text[m.end():m.end() + 100]):
            score += 3
        pre = text[:m.start()]
        if m.start() == 0 or pre.rstrip(" ").endswith("\n"):
            score += 1
            if text[m.start()] == "*":
                score += 1
        if score > best_score:
            best, best_score = m, score
    if require_entry and best_score <= 0:
        return None
    return best


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
            m = find_narr_entry(f["name"], narr_block, require_entry=True)
            if m:
                hits.append((m.start(), f))
        hits.sort()
        for idx, (start, f) in enumerate(hits):
            end = hits[idx + 1][0] if idx + 1 < len(hits) else len(narr_block)
            txt = re.sub(r"^\*\s*", "", collapse(narr_block[start:end]))
            # Fires without narratives appear in comma-separated name lists in
            # the PDF; slicing those yields junk like "Well," or "Dairy, and
            # other incidents." Real narratives always carry unit, location,
            # and behavior, so discard fragments barely longer than the name.
            if len(txt) < len(f["name"]) + 30:
                txt = ""
            f["narr"] = txt


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
        instr = p.get("instruction", "") or ""
        wind = re.search(r"WIND[.\s]*\.\.\.\s*([^\n]*)", desc)
        rh = re.search(r"RELATIVE HUMIDITY[.\s]*\.\.\.\s*([^\n]*)", desc)
        full_txt = desc
        if instr:
            full_txt += "\n\nPRECAUTIONARY/PREPAREDNESS ACTIONS...\n\n" + instr
        out.append({
            "event": p.get("event", ""), "area": p.get("areaDesc", ""),
            "headline": p.get("headline", ""),
            "office": p.get("senderName", "") or "",
            "onset": (p.get("onset") or "")[:16].replace("T", " "),
            "ends": (p.get("ends") or "")[:16].replace("T", " "),
            "wind": collapse(wind.group(1)) if wind else "",
            "rh": collapse(rh.group(1)) if rh else "",
            "full": reflow(full_txt),
        })
    return out


def fetch_inciweb_slugs():
    """Slugs of every incident page currently listed on InciWeb, so fire
    names can link only to pages that actually exist."""
    try:
        txt = http_get("https://inciweb.wildfire.gov/accessible-view")
    except Exception as e:  # noqa: BLE001
        print(f"  InciWeb fetch failed: {e}", file=sys.stderr)
        return set()
    return set(re.findall(r"/incident-information/([a-z0-9-]+)", txt))


def inciweb_url(f, slugs):
    """URL for a fire's InciWeb page (unitcode-name slug), or None."""
    if not slugs:
        return None
    name_slug = re.sub(r"-+", "-",
                       re.sub(r"[^a-z0-9]+", "-", f["name"].lower())).strip("-")
    unit_slug = re.sub(r"[^a-z0-9]", "", f["unit"].lower())
    exact = f"{unit_slug}-{name_slug}"
    if exact in slugs:
        return f"https://inciweb.wildfire.gov/incident-information/{exact}"
    cands = [s for s in slugs if s.endswith("-" + name_slug)]
    if len(cands) == 1:
        return f"https://inciweb.wildfire.gov/incident-information/{cands[0]}"
    return None


def fetch_spc():
    """Mirror the live SPC Day 1 product: risk levels (wind/RH and dry
    thunderstorm), the headline risk areas, and the full discussion text."""
    try:
        txt = http_get(SPC_URL)
    except Exception as e:  # noqa: BLE001
        print(f"  SPC fetch failed: {e}", file=sys.stderr)
        return {"available": False}

    valid = re.search(r"Valid\s+(\d{6}Z\s*-\s*\d{6}Z)", txt)
    issued = re.search(r"\d{3,4}\s+[AP]M\s+[A-Z]{2,4}\s+\w{3}\s+\w{3}\s+\d{2}\s+\d{4}", txt)
    no_risk = "No Risk Areas Forecast" in txt

    # Raw SPC text product. The page may or may not include the ZCZC/FNUS21
    # comms wrappers, so try several anchors against the tag-stripped page.
    stripped = html.unescape(re.sub(r"<[^>]+>", "", txt))
    prod = ""
    for pat in (r"ZCZC\s+SPCFWDDY1(.*?)(?:NNNN|\$\$|\Z)",
                r"FNUS21\s+KWNS\s+\d+(.*?)(?:NNNN|\$\$|\Z)",
                r"(Day 1 Fire Weather Outlook\s+NWS Storm Prediction Center"
                r".*?)(?:NNNN|\$\$|\Z)"):
        mp = re.search(pat, stripped, re.DOTALL)
        if mp:
            prod = mp.group(1)
            break
    # Cut anything after the forecaster signature (page footer links etc.).
    ms = re.search(r"\.\.[A-Za-z][\w\-. ]*\.\.\s*\d{2}/\d{2}/\d{4}", prod)
    if ms:
        prod = prod[:ms.end()]

    # Headline risk areas: "...CRITICAL FIRE WEATHER AREA FOR ... ..."
    headlines = []
    for m in re.finditer(r"\.\.\.(.+?)\.\.\.", prod, re.DOTALL):
        hl = collapse(m.group(1))
        if len(hl) > 12 and hl == hl.upper():
            headlines.append(hl)

    # Risk levels: prefer the page's risk-table cells, then headlines/prose.
    cells = " ".join(re.findall(
        r">\s*(Extremely\s+Critical|Extreme|Critical|Elevated|Dry\s+Tstm)\s*<",
        txt, re.IGNORECASE)).lower()
    blob = re.sub(r"\s+", " ", " ".join(headlines) + " " + prod).lower()
    scan = cells if cells else blob
    risk_level = None
    dry_tstm = None
    if not no_risk:
        if re.search(r"extremely critical|\bextreme\b", scan):
            risk_level = "EXTREMELY CRITICAL"
        elif "critical" in scan:
            risk_level = "CRITICAL"
        elif "elevated" in scan:
            risk_level = "ELEVATED"
        if "dry tstm" in scan or "dry thunderstorm" in blob:
            if "scattered dry thunderstorm" in blob:
                dry_tstm = "SCATTERED"
            elif "isolated dry thunderstorm" in blob:
                dry_tstm = "ISOLATED"
            else:
                dry_tstm = "DRY TSTM"

    # Full discussion: paragraphs after the Valid line, minus headline blocks
    # and the forecaster signature.
    body = ""
    if prod:
        tail = prod
        mv = re.search(r"Valid\s+\d{6}Z\s*-\s*\d{6}Z", tail)
        if mv:
            tail = tail[mv.end():]
        keep = []
        for chunk in re.split(r"\n\s*\n", tail):
            cc = collapse(chunk)
            if not cc or cc.startswith("...") or re.match(r"\.\.[A-Za-z]", cc):
                continue
            keep.append(cc)
        body = "\n\n".join(keep)

    return {"available": True, "no_risk": no_risk, "risk_level": risk_level,
            "dry_tstm": dry_tstm, "headlines": headlines,
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


def chip(label, bg, fg):
    return (f'<span style="background:{bg};color:{fg};font-weight:700;'
            f'padding:2px 10px;border-radius:12px;font-size:12px;'
            f'letter-spacing:0.6px;white-space:nowrap;'
            f'font-family:&quot;Oswald&quot;,sans-serif;">{esc(label)}</span>')


def ramp(pct):
    """Color for % of the ten-year average: greens below ~80, warming near
    100, reds darkening as the percentage climbs."""
    if pct is None:
        return "#8a8178"
    for lim, c in ((50, "#2e5339"), (65, "#4b6b34"), (80, "#6c733d"),
                   (90, "#bb8c4d"), (100, "#d58317"), (110, "#c1440e"),
                   (125, "#8f1d0e"), (150, "#5f0000")):
        if pct < lim:
            return c
    return "#3d0000"


def tint(c, a):
    return f"rgba({int(c[1:3], 16)},{int(c[3:5], 16)},{int(c[5:7], 16)},{a})"


def fmt_when(a):
    """Compact 'Jul 25, 11:00 AM - Jul 25, 10:00 PM PDT' from onset/ends."""
    def one(s):
        try:
            dt = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return s or ""
        return f'{dt.strftime("%b")} {dt.day}, {dt.strftime("%I:%M %p").lstrip("0")}'
    parts = [one(a["onset"]), one(a["ends"])]
    when = " - ".join(p for p in parts if p)
    tz = re.search(r"\b([A-Z]{2,3}T)\b", a.get("headline", ""))
    return when + (f' {tz.group(1)}' if tz and when else "")


def spc_rows(spc):
    """(chip label, chip bg, chip fg, box class/style, area text) per headline."""
    rows = []
    for hl in spc.get("headlines", []):
        h = collapse(hl)
        m = re.match(r"(EXTREMELY CRITICAL|CRITICAL|ELEVATED)\s+"
                     r"FIRE WEATHER AREA\s+(?:FOR\s+)?(.*)", h)
        rest = m.group(2) if m else h
        level = m.group(1) if m else (spc.get("risk_level") or "RISK")
        dm = re.match(r"(SCATTERED|ISOLATED)?\s*DRY THUNDERSTORMS?\s*"
                      r"(?:ACROSS|FOR|OVER)?\s*(.*)", rest)
        if dm and "DRY THUNDERSTORM" in rest:
            qual = ((dm.group(1) + " ") if dm.group(1) else "") + "DRY T-STORMS"
            area = dm.group(2).strip() or rest
            rows.append((qual, "#280069", "#f5efe6", "tstm", area))
        elif level == "ELEVATED":
            rows.append((level, "#bb8c4d", "#2f292b", "fww", rest))
        elif level == "EXTREMELY CRITICAL":
            rows.append((level, "#3d0000", "#f5efe6", "rfw", rest))
        else:
            rows.append((level, "#5f0000", "#f5efe6", "rfw", rest))
    return rows


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


def render(data, nws, spc, inciweb=None):
    d = data
    inciweb = inciweb or set()
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

    # Fire weather at a glance: SPC verdict + alert counts, linking down
    rfw_ct = sum(1 for a in nws if a["event"] == "Red Flag Warning")
    fww_ct = sum(1 for a in nws if a["event"] == "Fire Weather Watch")
    other_ct = len(nws) - rfw_ct - fww_ct
    H.append('<div class="wxcards" id="wx-glance">')
    if not spc.get("available"):
        H.append('<a class="stat-a" href="#spc"><div class="stat"><div class="n">N/A</div>'
                 '<div class="l">SPC Day 1 Outlook</div></div></a>')
    elif spc.get("no_risk"):
        H.append('<a class="stat-a" href="#spc"><div class="stat wx-ok">'
                 '<div class="n">NO RISK</div><div class="l">SPC Day 1 Outlook</div></div></a>')
    else:
        level = spc.get("risk_level") or "RISK"
        lcls = "wx-fww" if level == "ELEVATED" else "wx-rfw"
        lsize = ' style="font-size:24px;line-height:38px;"' if len(level) > 8 else ''
        H.append(f'<a class="stat-a" href="#spc"><div class="stat {lcls}">'
                 f'<div class="n"{lsize}>{level}</div>'
                 f'<div class="l">SPC Day 1 Outlook</div></div></a>')
    if spc.get("dry_tstm") and not spc.get("no_risk"):
        dt = spc["dry_tstm"]
        dsize = ' style="font-size:24px;line-height:38px;"' if len(dt) > 8 else ''
        H.append(f'<a class="stat-a" href="#spc"><div class="stat wx-tstm">'
                 f'<div class="n"{dsize}>{dt}</div>'
                 f'<div class="l">Dry Thunderstorms</div></div></a>')
    H.append(f'<a class="stat-a" href="#nws"><div class="stat {"wx-rfw" if rfw_ct else "wx-ok"}">'
             f'<div class="n">{rfw_ct}</div><div class="l">Red Flag Warnings</div></div></a>')
    H.append(f'<a class="stat-a" href="#nws"><div class="stat {"wx-fww" if fww_ct else "wx-ok"}">'
             f'<div class="n">{fww_ct}</div><div class="l">Fire Weather Watches</div></div></a>')
    if other_ct:
        H.append(f'<a class="stat-a" href="#nws"><div class="stat wx-rfw">'
                 f'<div class="n">{other_ct}</div><div class="l">Other Fire Alerts</div></div></a>')
    H.append('</div>')

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

    # Year-to-date totals vs the ten-year average, on a severity color ramp
    if d.get("ytd"):
        y = d["ytd"]
        H.append('<div class="stats" style="grid-template-columns:'
                 'repeat(auto-fit,minmax(240px,1fr));margin-bottom:6px;">')
        for key, pkey, label in (("fires", "fires_pct", "Fires Year to Date"),
                                 ("acres", "acres_pct", "Acres Year to Date")):
            pct = y.get(pkey)
            col = ramp(pct)
            card = (f'<div class="stat" style="background:{tint(col, 0.08)};'
                    f'border-color:{col};"><div class="n" style="color:{col};">'
                    f'{esc(y[key])}</div><div class="l">{label}</div>')
            if pct is not None:
                card += (f'<div style="margin-top:6px;font-size:13px;">'
                         f'<span style="color:{col};font-weight:700;">{pct}%</span> '
                         f'<span class="muted">of the 10-year average</span></div>')
            card += '</div>'
            H.append(f'<a class="stat-a" href="#gacc-levels">{card}</a>')
        H.append('</div>')

    H.append('<h2 id="gacc-levels">GACC Preparedness Levels</h2><div class="gaccgrid">')
    for code in GACC_CELL_ORDER:
        summ = d["gacc_summary"].get(code, {})
        pl = summ.get("pl", 1)
        cell = (f'<div class="gcell"><div><div class="code">{code}</div>'
                f'<div class="full">{esc(GACC_SHORT[code])}</div>'
                f'<div style="font-size:12px;color:#8a8178;margin-top:4px;">'
                f'<b style="color:#d58317;">{summ.get("incidents", 0)}</b> fires</div>'
                f'<div style="font-size:12px;color:#8a8178;">'
                f'<b style="color:#d58317;">{esc(summ.get("acres", "0"))}</b> acres</div>'
                f'</div>{pl_badge(pl)}</div>')
        if code in linked_codes:
            cell = (f'<a class="gcell-a" href="#gacc-{code}" '
                    f'title="Jump to {code} large fires">{cell}</a>')
        H.append(cell)
    H.append('</div>')

    extra = (f' (plus {new_contained_ct} reported new, already contained)'
             if new_contained_ct else '')
    H.append(f'<h2 id="new-fires">New Large Fires <span class="sub">— {len(new_active)} '
             f'active new incidents in {", ".join(new_states) or "—"}{extra}</span></h2>')
    # Fires with narratives first (by size); no-narrative fires at the end.
    for f in sorted(new_active, key=lambda x: (not x["narr"], -acnum(x["acres"]))):
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

    H.append('<h2 id="fire-weather">Fire Weather</h2>')

    H.append('<h3 id="weather">Predictive Services Discussion</h3><div class="card wxtext">')
    for para in (d["weather"] or "N/A").split("\n\n"):
        H.append(f'<p>{esc(para)}</p>')
    H.append('</div>')

    H.append('<h3 id="spc">SPC Day 1 Fire Weather Outlook</h3>')
    if not spc.get("available"):
        H.append('<div class="alert spc-elevated">Live SPC product was unavailable at '
                 'generation time.</div>')
    else:
        meta = []
        if spc.get("issued"):
            meta.append(f'Issued {esc(spc["issued"])}')
        if spc.get("valid"):
            meta.append(f'Valid {esc(spc["valid"])}')
        meta.append('Mirrored live from spc.noaa.gov')
        H.append(f'<div class="muted" style="font-size:13px;margin-bottom:10px;">'
                 f'{" &nbsp;·&nbsp; ".join(meta)}</div>')
        if spc.get("no_risk"):
            H.append('<div class="banner-ok"><b>No Risk Areas Forecast.</b> '
                     'The outlook delineates no Critical or Elevated risk areas.</div>')
        else:
            rows = spc_rows(spc)
            if not rows:
                bits = []
                if spc.get("risk_level"):
                    bits.append(f'{spc["risk_level"].title()} fire weather area')
                if spc.get("dry_tstm"):
                    bits.append('Dry thunderstorms')
                H.append(f'<div class="alert spc-critical"><b>'
                         f'{esc(" · ".join(bits) or "Risk areas")} forecast.</b></div>')
            for label, bg, fg, kind, area in rows:
                box = ('<div class="alert" style="background:rgba(40,0,105,0.06);'
                       'border:1px solid #280069;border-left:5px solid #280069;">'
                       if kind == "tstm" else f'<div class="alert {kind}">')
                H.append(box + '<div class="atitle" style="display:flex;'
                         'align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:0;">'
                         + chip(label, bg, fg)
                         + f'<span style="font-weight:600;font-size:14px;'
                         f'letter-spacing:0.4px;">{esc(area)}</span></div></div>')
        if spc.get("body"):
            H.append('<div class="card wxtext">')
            for para in spc["body"].split("\n\n"):
                H.append(f'<p>{esc(para)}</p>')
            H.append('</div>')

    H.append('<h3 id="nws">NWS Fire Weather Alerts</h3>')
    if not nws:
        H.append('<div class="banner-ok">No active Red Flag Warnings or Fire Weather Watches.</div>')
    for a in nws:
        if a["event"] == "Fire Weather Watch":
            cls, cbg, cfg = "fww", "#bb8c4d", "#2f292b"
        else:
            cls, cbg, cfg = "rfw", "#5f0000", "#f5efe6"
        H.append(f'<div class="alert {cls}">'
                 f'<div class="atitle" style="display:flex;align-items:flex-start;'
                 f'gap:10px;flex-wrap:wrap;">{chip(a["event"].upper(), cbg, cfg)}'
                 f'<span style="font-weight:600;font-size:14px;flex:1;'
                 f'min-width:200px;">{esc(a["area"])}</span></div>')
        det = []
        when = fmt_when(a)
        if when:
            det.append(f'<b>When:</b> {esc(when)}')
        if a["wind"]:
            det.append(f'<b>Winds:</b> {esc(a["wind"])}')
        if a["rh"]:
            det.append(f'<b>Min RH:</b> {esc(a["rh"])}')
        if a.get("office"):
            det.append(f'<span class="muted">{esc(a["office"])}</span>')
        H.append(f'<div style="font-size:13px;margin-top:4px;">'
                 f'{" &nbsp;·&nbsp; ".join(det)}</div>')
        if a.get("full"):
            H.append('<details style="margin-top:9px;"><summary style="cursor:pointer;'
                     'color:#074259;font-size:13px;font-weight:600;">Full alert text'
                     f'</summary><div style="white-space:pre-wrap;font-size:13px;'
                     f'color:#4a443f;margin-top:8px;border-top:1px solid #e8e1d4;'
                     f'padding-top:8px;">{esc(a["full"])}</div></details>')
        H.append('</div>')

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
        for f in sorted(gfires, key=lambda x: (not x["narr"], -acnum(x["acres"]))):
            ncls = " new" if f["new"] else ""
            nb = " " + new_badge() if f["new"] else ""
            url = inciweb_url(f, inciweb)
            name_html = (f'<a href="{url}" target="_blank">{esc(f["name"])}</a>'
                         if url else esc(f["name"]))
            H.append(f'<div class="fire{ncls}"><div class="fname">{name_html} '
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
h3{font-family:"Oswald","Arial Narrow",sans-serif;font-size:17px;font-weight:600;letter-spacing:0.4px;margin:26px 0 12px;padding-bottom:6px;border-bottom:1px solid #d5cdbf;color:#5f0000;scroll-margin-top:12px}
.wxcards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:14px}
.wx-ok{background:rgba(108,115,61,0.10);border-color:#6c733d}
.wx-ok .n{color:#535c2b}
.wx-rfw{background:rgba(95,0,0,0.06);border-color:#5f0000}
.wx-rfw .n{color:#5f0000}
.wx-fww{background:rgba(187,140,77,0.12);border-color:#bb8c4d}
.wx-fww .n{color:#8a5f22}
.wx-tstm{background:rgba(40,0,105,0.06);border-color:#280069}
.wx-tstm .n{color:#280069}
.gcell-a{text-decoration:none;color:#2f292b;display:block}
.gcell-a .gcell{transition:border-color 0.15s, box-shadow 0.15s}
.gcell-a:hover .gcell{border-color:#5f0000;box-shadow:0 1px 6px rgba(95,0,0,0.18)}
.gaccgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.gcell{background:#fff;border:1px solid #ddd5c7;border-radius:10px;padding:12px;display:flex;justify-content:space-between;align-items:flex-start;gap:8px;min-height:118px;height:100%}
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
    print("Fetching InciWeb incident list...")
    inciweb = fetch_inciweb_slugs()
    print(f"  {len(inciweb)} InciWeb incident pages")

    html_out = render(data, nws, spc, inciweb)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(html_out)
    print(f"Wrote {len(html_out)} bytes -> {OUTPUT_PATH}")
    print(f"Parsed {len(data['fires'])} fires "
          f"({sum(1 for f in data['fires'] if f['new'])} new, "
          f"{sum(1 for f in data['fires'] if f['pct']=='100')} contained)")


if __name__ == "__main__":
    main()
