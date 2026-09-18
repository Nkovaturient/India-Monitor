"""Selective HTML fetchers for rankings with relatively stable public pages.

Each fetcher returns {"rank_label": str, "note": str} or None if India cannot
be parsed confidently. Never invent a rank.
"""

import re

from bs4 import BeautifulSoup

from common import fetch_url, log

INDIA_RX = re.compile(r"\bindia\b", re.I)
RANK_NEAR_RX = re.compile(
    r"(?:rank(?:ed|ing)?[:\s#]*)?(\d{1,3})(?:\s*/\s*(\d{1,3}))?",
    re.I,
)


def _html(url):
    raw = fetch_url(url, timeout=25)
    return BeautifulSoup(raw, "lxml")


def _text(soup):
    return soup.get_text(" ", strip=True)


def _india_rank_from_text(text, max_window=80):
    """Find 'India' then a nearby rank number. Conservative: first plausible hit."""
    for m in INDIA_RX.finditer(text):
        window = text[m.start(): m.start() + max_window]
        rm = RANK_NEAR_RX.search(window)
        if not rm:
            before = text[max(0, m.start() - max_window): m.start()]
            rm = RANK_NEAR_RX.search(before)
        if not rm:
            continue
        n = int(rm.group(1))
        if n < 1 or n > 250:
            continue
        total = rm.group(2)
        if total:
            return f"{n} / {int(total)}"
        suffix = "th"
        if 10 <= n % 100 <= 20:
            suffix = "th"
        else:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n}{suffix}"
    return None


def _row_rank(soup, name="india"):
    for tr in soup.select("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if not cells:
            continue
        blob = " ".join(cells)
        if not INDIA_RX.search(blob):
            continue
        for cell in cells:
            m = re.match(r"^#?(\d{1,3})(?:\s*/\s*(\d{1,3}))?$", cell.strip())
            if m and 1 <= int(m.group(1)) <= 250:
                if m.group(2):
                    return f"{int(m.group(1))} / {int(m.group(2))}"
                n = int(m.group(1))
                suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th") if not (10 <= n % 100 <= 20) else "th"
                return f"{n}{suffix}"
        rm = RANK_NEAR_RX.search(blob)
        if rm and 1 <= int(rm.group(1)) <= 250:
            n = int(rm.group(1))
            if rm.group(2):
                return f"{n} / {int(rm.group(2))}"
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th") if not (10 <= n % 100 <= 20) else "th"
            return f"{n}{suffix}"
    return None


def fetch_passport(entry):
    soup = _html(entry["source_url"])
    label = _row_rank(soup) or _india_rank_from_text(_text(soup))
    if not label:
        log("  ! passport: could not parse India rank")
        return None
    return {"rank_label": label, "note": "Parsed from Henley Passport Index ranking table."}


def fetch_happiness(entry):
    soup = _html(entry["source_url"])
    label = _row_rank(soup) or _india_rank_from_text(_text(soup))
    if not label:
        log("  ! happiness: could not parse India rank")
        return None
    return {"rank_label": label, "note": "Parsed from World Happiness Report page."}


def fetch_press(entry):
    soup = _html(entry["source_url"])
    label = _row_rank(soup) or _india_rank_from_text(_text(soup), max_window=120)
    if not label:
        log("  ! press: could not parse India rank")
        return None
    return {"rank_label": label, "note": "Parsed from RSF World Press Freedom Index."}


def fetch_cpi(entry):
    soup = _html(entry["source_url"])
    label = _row_rank(soup) or _india_rank_from_text(_text(soup))
    if not label:
        log("  ! cpi: could not parse India rank")
        return None
    return {"rank_label": label, "note": "Parsed from Transparency International CPI."}


def fetch_ghi(entry):
    soup = _html(entry["source_url"])
    label = _row_rank(soup) or _india_rank_from_text(_text(soup))
    if not label:
        log("  ! ghi: could not parse India rank")
        return None
    return {"rank_label": label, "note": "Parsed from Global Hunger Index ranking."}


def fetch_gii(entry):
    soup = _html(entry["source_url"])
    label = _row_rank(soup) or _india_rank_from_text(_text(soup))
    if not label:
        log("  ! gii: could not parse India rank")
        return None
    return {"rank_label": label, "note": "Parsed from WIPO Global Innovation Index."}


def fetch_aqi(entry):
    soup = _html(entry["source_url"])
    label = _row_rank(soup) or _india_rank_from_text(_text(soup))
    if not label:
        log("  ! aqi: could not parse India rank")
        return None
    return {"rank_label": label, "note": "Parsed from IQAir most-polluted countries list."}


FETCHERS = {
    "fetch_passport": fetch_passport,
    "fetch_happiness": fetch_happiness,
    "fetch_press": fetch_press,
    "fetch_cpi": fetch_cpi,
    "fetch_ghi": fetch_ghi,
    "fetch_gii": fetch_gii,
    "fetch_aqi": fetch_aqi,
}


def run_fetcher(entry):
    name = entry.get("fetcher")
    fn = FETCHERS.get(name) if name else None
    if not fn:
        return None
    try:
        return fn(entry)
    except Exception as e:
        log(f"  ! fetcher {name} failed: {e}")
        return None
