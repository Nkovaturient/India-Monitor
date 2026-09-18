#!/usr/bin/env python3
"""
India Monitor — budget_parse.py

Reads a Union Budget Expenditure Profile PDF (BUDGET_PDF_URL) and upserts
matched budget_facts / budget_sectors rows. Unmatched slugs are left alone.
On parse failure the existing seed data is not wiped.

  python scripts/budget_parse.py --dry-run
"""

import argparse
import io
import os
import re
import sys

import pdfplumber

from common import fetch_url, log, record_health, supabase_post

FACT_PATTERNS = [
    ("total", re.compile(r"total expenditure", re.I)),
    ("capex", re.compile(r"capital expenditure", re.I)),
    ("deficit", re.compile(r"fiscal deficit", re.I)),
    ("debt", re.compile(r"debt[- ]to[- ]gdp|debt.?gdp", re.I)),
]

SECTOR_PATTERNS = [
    ("interest", re.compile(r"interest payments|debt servicing", re.I)),
    ("defence", re.compile(r"^defence$|ministry of defence", re.I)),
    ("roads", re.compile(r"road transport|highways", re.I)),
    ("rail", re.compile(r"^railways$|ministry of railways", re.I)),
    ("home", re.compile(r"home affairs", re.I)),
    ("pension", re.compile(r"^pensions?$", re.I)),
    ("education", re.compile(r"education|school education|higher education", re.I)),
    ("health", re.compile(r"health( and family)?|ministry of health", re.I)),
]

CRORE_RX = re.compile(r"([\d,]+(?:\.\d+)?)\s*(?:lakh crore|lakh cr|crore|cr)?", re.I)
PCT_RX = re.compile(r"(\d+(?:\.\d+)?)\s*%")
NUM_RX = re.compile(r"[\d,]+(?:\.\d+)?")


def fmt_lakh_cr(crore):
    lakh = crore / 100000.0
    if lakh >= 10:
        return f"₹{lakh:.2f}L cr"
    if lakh >= 1:
        return f"₹{lakh:.2f}L cr"
    return f"₹{crore:,.0f} cr"


def parse_number(text):
    if not text:
        return None
    cleaned = text.replace(",", "").strip()
    m = NUM_RX.search(cleaned)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def cell_blob(row):
    return " ".join((c or "").strip() for c in row if c)


def extract_tables(pdf_bytes):
    rows = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages[:40]:
            for table in (page.extract_tables() or []):
                for row in table:
                    if not row:
                        continue
                    cells = [(c or "").replace("\n", " ").strip() for c in row]
                    if any(cells):
                        rows.append(cells)
    return rows


def match_slug(text, patterns):
    for slug, rx in patterns:
        if rx.search(text or ""):
            return slug
    return None


def pick_amount(cells):
    """Prefer the last numeric cell (usually BE of the latest year)."""
    nums = []
    for cell in cells:
        n = parse_number(cell)
        if n is not None and n > 0:
            nums.append(n)
    return nums[-1] if nums else None


def build_updates(table_rows):
    facts = {}
    sectors = {}
    for cells in table_rows:
        blob = cell_blob(cells)
        fact_slug = match_slug(blob, FACT_PATTERNS)
        if fact_slug and fact_slug not in facts:
            pct = PCT_RX.search(blob)
            amount = pick_amount(cells)
            if fact_slug in ("deficit", "debt") and pct:
                facts[fact_slug] = {"value": f"{pct.group(1)}%"}
            elif amount is not None:
                # Expenditure Profile figures are typically ₹ crore.
                facts[fact_slug] = {"value": fmt_lakh_cr(amount)}
        sec_slug = match_slug(blob, SECTOR_PATTERNS)
        if sec_slug and sec_slug not in sectors:
            amount = pick_amount(cells)
            if amount is None:
                continue
            sectors[sec_slug] = {"amount_cr": amount}

    total = None
    if "total" in facts:
        # reverse fmt is hard; keep pct relative if we also captured total row amount
        pass
    for cells in table_rows:
        blob = cell_blob(cells)
        if re.search(r"total expenditure through budget|9\.\s*total expenditure", blob, re.I):
            total = pick_amount(cells)
            if total:
                facts.setdefault("total", {"value": fmt_lakh_cr(total)})
            break
    if total:
        for slug, rec in sectors.items():
            pct = round(rec["amount_cr"] / total * 100, 1)
            rec["pct"] = pct
            rec["value_label"] = f"{fmt_lakh_cr(rec['amount_cr'])} · {pct}%"
    else:
        for slug, rec in list(sectors.items()):
            rec["value_label"] = fmt_lakh_cr(rec["amount_cr"])
            rec.pop("pct", None)

    if "other" not in sectors and total and sectors:
        used = sum(r["amount_cr"] for r in sectors.values())
        remainder = max(total - used, 0)
        pct = round(remainder / total * 100, 1) if total else None
        sectors["other"] = {
            "amount_cr": remainder,
            "pct": pct if pct is not None else 33,
            "value_label": f"remainder · ~{pct}%" if pct is not None else "remainder",
        }
    return facts, sectors


def main():
    parser = argparse.ArgumentParser(description="Parse Union Budget PDF into budget_* tables.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--url", help="Override BUDGET_PDF_URL")
    args = parser.parse_args()

    url = args.url or os.environ.get("BUDGET_PDF_URL", "").strip()
    if not url:
        log("BUDGET_PDF_URL is not set — skipping (seeded budget figures left unchanged).")
        if not args.dry_run:
            record_health("Budget · PDF parse", ok=True, items_seen=0, items_kept=0,
                          error="BUDGET_PDF_URL unset; no-op")
        return 0

    log(f"=== Budget PDF parse: {url} ===")
    try:
        pdf_bytes = fetch_url(url, accept="application/pdf,*/*;q=0.8", timeout=60)
    except Exception as e:
        log(f"  ! fetch failed: {e}")
        if not args.dry_run:
            record_health("Budget · PDF parse", ok=False, error=str(e))
        return 1

    try:
        tables = extract_tables(pdf_bytes)
        facts, sectors = build_updates(tables)
    except Exception as e:
        log(f"  ! parse failed: {e}")
        if not args.dry_run:
            record_health("Budget · PDF parse", ok=False, error=str(e))
        return 1

    log(f"  matched {len(facts)} facts, {len(sectors)} sectors from {len(tables)} table rows.")
    for slug, rec in facts.items():
        log(f"    fact {slug}: {rec['value']}")
    for slug, rec in sectors.items():
        log(f"    sector {slug}: {rec.get('value_label')} pct={rec.get('pct')}")

    if args.dry_run:
        return 0
    if not facts and not sectors:
        record_health("Budget · PDF parse", ok=False, items_seen=len(tables),
                      error="no rows matched known slugs")
        return 1

    kept = 0
    fact_rows = [{"slug": slug, **rec} for slug, rec in facts.items()]
    if fact_rows:
        kept += supabase_post("budget_facts", fact_rows, on_conflict="slug", merge=True)
    sector_rows = []
    for slug, rec in sectors.items():
        row = {"slug": slug, "value_label": rec.get("value_label")}
        if rec.get("pct") is not None:
            row["pct"] = rec["pct"]
        sector_rows.append(row)
    if sector_rows:
        kept += supabase_post("budget_sectors", sector_rows, on_conflict="slug", merge=True)
    record_health("Budget · PDF parse", ok=True, items_seen=len(tables), items_kept=kept)
    log("=== Done. ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
