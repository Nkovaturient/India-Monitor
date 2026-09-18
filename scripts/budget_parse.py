#!/usr/bin/env python3
"""
India Monitor — budget_parse.py

Reads a Union Budget Expenditure Profile PDF (BUDGET_PDF_URL) and upserts
matched budget_facts / budget_sectors rows. Unmatched slugs are left alone.
On parse failure the existing seed data is not wiped.

pdfminer (used by pdfplumber) raises AssertionError('Unhandled', 12) on
Statement 1 / Statement 3 pages in vol1.pdf — text is extracted with pypdf.

  python scripts/budget_parse.py --dry-run --url https://www.indiabudget.gov.in/doc/eb/vol1.pdf
"""

import argparse
import io
import json as _json
import os
import re
import sys
import time as _time
import traceback

import requests
from pypdf import PdfReader

from common import FETCH_HEADERS, log, record_health, supabase_post

# #region agent log
_DBG_PATH = "/Users/matrix/Agentic ✅/.cursor/debug-f72722.log"


def _dbg(hypothesis_id, location, message, data=None, run_id="run1"):
    try:
        with open(_DBG_PATH, "a", encoding="utf-8") as f:
            f.write(_json.dumps({
                "sessionId": "f72722",
                "runId": run_id,
                "hypothesisId": hypothesis_id,
                "location": location,
                "message": message,
                "data": data or {},
                "timestamp": int(_time.time() * 1000),
            }) + "\n")
    except Exception:
        pass
# #endregion

SECTOR_DEMANDS = {
    "interest": ["Interest Payments"],
    "defence": [
        "Ministry of Defence (Civil)",
        "Defence Services (Revenue)",
        "Capital Outlay on Defence Services",
        "Defence Pensions",
    ],
    "roads": ["Ministry of Road Transport and Highways"],
    "rail": ["Ministry of Railways"],
    "home": ["Ministry of Home Affairs", "Police"],
    "pension": ["Pensions"],
    "education": [
        "Department of School Education and Literacy",
        "Department of Higher Education",
    ],
    "health": ["Department of Health and Family Welfare"],
}

AMOUNT_TOK = re.compile(r"^(?:[\d,]+(?:\.\d+)?|\.{3})$")


def fmt_lakh_cr(crore):
    lakh = crore / 100000.0
    if lakh >= 1:
        return f"₹{lakh:.2f}L cr"
    return f"₹{crore:,.0f} cr"


def download_pdf(url):
    headers = dict(FETCH_HEADERS)
    headers["Accept"] = "application/pdf,*/*;q=0.8"
    resp = requests.get(url, headers=headers, timeout=60, allow_redirects=True)
    content = resp.content or b""
    content_type = resp.headers.get("content-type", "")
    # #region agent log
    _dbg("A", "budget_parse.py:download_pdf", "download response", {
        "status": resp.status_code,
        "final_url": (resp.url or "")[:180],
        "content_type": content_type,
        "nbytes": len(content),
        "magic": content[:8].decode("latin-1", errors="replace"),
        "is_pdf": content.startswith(b"%PDF-"),
        "redirects": len(resp.history),
    }, run_id="post-fix")
    # #endregion
    if resp.status_code != 200 or not content:
        raise RuntimeError(f"HTTP {resp.status_code}, {len(content)} bytes, type={content_type}")
    if not content.startswith(b"%PDF-"):
        preview = content[:200].decode("utf-8", errors="replace")
        raise ValueError(
            f"URL did not return a PDF: status={resp.status_code}, "
            f"content_type={content_type}, preview={preview!r}"
        )
    return content, content_type, resp.url


def last_amount(text):
    nums = []
    for tok in (text or "").replace(",", "").split():
        if tok == "..." or set(tok) == {"."}:
            continue
        try:
            nums.append(float(tok))
        except ValueError:
            continue
    return nums[-1] if nums else None


def extract_text_pypdf(pdf_bytes, max_pages=90):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    # #region agent log
    _dbg("C", "budget_parse.py:extract_text_pypdf", "pypdf opened", {
        "pages": len(reader.pages),
        "encrypted": bool(getattr(reader, "is_encrypted", False)),
    }, run_id="post-fix")
    # #endregion
    chunks = []
    limit = min(len(reader.pages), max_pages)
    for i in range(limit):
        try:
            t = reader.pages[i].extract_text() or ""
        except Exception as e:
            log(f"  ! pypdf page {i + 1} failed: {e!r}")
            # #region agent log
            _dbg("C", "budget_parse.py:extract_text_pypdf", "page failed", {
                "page": i + 1, "error_type": type(e).__name__, "error": str(e)[:200],
            }, run_id="post-fix")
            # #endregion
            continue
        chunks.append(t)
    return "\n".join(chunks)


def parse_statement1(text):
    facts = {}
    m = re.search(r"9\.\s*Total Expenditure through Budget[^\n]*", text, re.I)
    if not m:
        return facts
    nums = []
    for tok in m.group(0).replace(",", "").split():
        try:
            nums.append(float(tok))
        except ValueError:
            continue
    if len(nums) >= 3:
        facts["total"] = {"value": fmt_lakh_cr(nums[-1]), "amount_cr": nums[-1]}
        facts["capex"] = {"value": fmt_lakh_cr(nums[-2]), "amount_cr": nums[-2]}
    elif nums:
        facts["total"] = {"value": fmt_lakh_cr(nums[-1]), "amount_cr": nums[-1]}
    return facts


def parse_demands(text):
    found = {}
    parts = re.split(r"(?=Demand No\.\s*\d+)", text)
    for part in parts:
        m = re.match(r"Demand No\.\s*\d+\s*\n(.+)", part, re.S | re.I)
        if not m:
            continue
        rest = m.group(1).strip()
        first, _, after = rest.partition("\n")
        tokens = first.split()
        split_at = None
        for i, tok in enumerate(tokens):
            if AMOUNT_TOK.match(tok):
                split_at = i
                break
        if split_at is None:
            title = first.strip()
            amount_line = after.split("\n", 1)[0]
        else:
            title = " ".join(tokens[:split_at]).strip()
            amount_line = " ".join(tokens[split_at:])
        if not title or title.startswith("1."):
            continue
        amt = last_amount(amount_line)
        if amt is None:
            continue
        found[title] = amt
    return found


def build_updates_from_text(text):
    facts = parse_statement1(text)
    demands = parse_demands(text)
    sectors = {}
    for slug, titles in SECTOR_DEMANDS.items():
        total = 0.0
        matched = False
        wanted = {t.lower() for t in titles}
        for name, amt in demands.items():
            if name.strip().lower() in wanted:
                total += amt
                matched = True
        if matched:
            sectors[slug] = {"amount_cr": total}

    grand = facts.get("total", {}).get("amount_cr")
    if grand:
        for rec in sectors.values():
            pct = round(rec["amount_cr"] / grand * 100, 1)
            rec["pct"] = pct
            rec["value_label"] = f"{fmt_lakh_cr(rec['amount_cr'])} · {pct}%"
    else:
        for rec in sectors.values():
            rec["value_label"] = fmt_lakh_cr(rec["amount_cr"])

    if "other" not in sectors and grand and sectors:
        used = sum(r["amount_cr"] for r in sectors.values())
        remainder = max(grand - used, 0)
        pct = round(remainder / grand * 100, 1)
        sectors["other"] = {
            "amount_cr": remainder,
            "pct": pct,
            "value_label": f"remainder · ~{pct}%",
        }
    # #region agent log
    _dbg("C", "budget_parse.py:build_updates_from_text", "parsed", {
        "facts": {k: v.get("value") for k, v in facts.items()},
        "sectors": {k: v.get("value_label") for k, v in sectors.items()},
        "demand_count": len(demands),
    }, run_id="post-fix")
    # #endregion
    return facts, sectors, len(demands)


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
        pdf_bytes, content_type, final_url = download_pdf(url)
        log(f"  downloaded {len(pdf_bytes)} bytes ({content_type}) from {final_url}")
    except Exception as e:
        # #region agent log
        _dbg("A", "budget_parse.py:main", "fetch failed", {
            "error_type": type(e).__name__,
            "error": str(e)[:400],
        }, run_id="post-fix")
        # #endregion
        log(f"  ! fetch failed: {e}")
        if not args.dry_run:
            record_health("Budget · PDF parse", ok=False, error=str(e)[:500])
        return 1

    try:
        text = extract_text_pypdf(pdf_bytes)
        facts, sectors, demand_n = build_updates_from_text(text)
    except Exception as e:
        tb = traceback.format_exc()
        # #region agent log
        _dbg("C", "budget_parse.py:main", "parse failed", {
            "error_type": type(e).__name__,
            "error": str(e)[:400],
            "traceback_tail": tb[-800:],
        }, run_id="post-fix")
        # #endregion
        log(f"  ! parse failed: {e!r}\n{tb}")
        if not args.dry_run:
            record_health("Budget · PDF parse", ok=False, error=f"{type(e).__name__}: {e}"[:500])
        return 1

    log(f"  matched {len(facts)} facts, {len(sectors)} sectors from {demand_n} demand blocks.")
    for slug, rec in facts.items():
        log(f"    fact {slug}: {rec['value']}")
    for slug, rec in sectors.items():
        log(f"    sector {slug}: {rec.get('value_label')} pct={rec.get('pct')}")

    if args.dry_run:
        return 0
    if not facts and not sectors:
        record_health("Budget · PDF parse", ok=False, items_seen=demand_n,
                      error="no rows matched known slugs")
        return 1

    kept = 0
    fact_rows = [{"slug": slug, "value": rec["value"]} for slug, rec in facts.items() if rec.get("value")]
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
    record_health("Budget · PDF parse", ok=True, items_seen=demand_n, items_kept=kept)
    log("=== Done. ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
