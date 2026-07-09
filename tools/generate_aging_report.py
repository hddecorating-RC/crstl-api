"""
generate_aging_report.py
------------------------
Reads .tmp/invoices_raw.json (output of crstl_fetch_invoices.py),
builds an aging report, and pushes it to Google Sheets.

Two tabs are written:
  - "Aging Detail"  : one row per outstanding invoice, color-coded by bucket
  - "Summary"       : bucket totals with grand total row

Usage:
    python tools/generate_aging_report.py

Prerequisites:
    - Run tools/crstl_fetch_invoices.py first
    - Set GOOGLE_SHEETS_URL in .env
    - Place credentials.json (Google service account or OAuth) in project root
"""

import os
import sys
import json
import pathlib
from datetime import date

import gspread
from gspread_formatting import (
    CellFormat, Color, TextFormat, format_cell_range,
    set_frozen, get_effective_format,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_env(path=".env"):
    env_file = pathlib.Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


load_env()

SHEETS_URL   = os.environ.get("GOOGLE_SHEETS_URL", "")
INPUT_PATH   = pathlib.Path(".tmp/invoices_raw.json")
CREDENTIALS  = pathlib.Path("credentials.json")
TOKEN        = pathlib.Path("token.json")

AGING_BUCKETS = ["current", "1-30", "31-60", "61-90", "90+"]

# Column definitions for Aging Detail tab
DETAIL_HEADERS = [
    "Invoice #",
    "PO #",
    "Retailer",
    "Issue Date",
    "Due Date",
    "Total ($)",
    "Paid ($)",
    "Outstanding ($)",
    "Days Overdue",
    "Bucket",
]

# Row background colors per bucket (RGB 0-1 scale)
BUCKET_COLORS = {
    "90+":     Color(1.0,  0.8,  0.8),   # red tint
    "61-90":   Color(1.0,  0.9,  0.8),   # orange tint
    "31-60":   Color(1.0,  0.97, 0.8),   # yellow tint
    "1-30":    Color(1.0,  1.0,  1.0),   # white
    "current": Color(0.94, 1.0,  0.94),  # light green
}

HEADER_BG    = Color(0.12, 0.31, 0.47)   # dark blue
HEADER_TEXT  = Color(1.0,  1.0,  1.0)   # white
MONEY_COLS   = [6, 7, 8]                 # 1-indexed: Total, Paid, Outstanding
DATE_COLS    = [4, 5]                    # 1-indexed: Issue Date, Due Date


# ---------------------------------------------------------------------------
# Google Sheets auth
# ---------------------------------------------------------------------------

def get_client():
    if CREDENTIALS.exists():
        # Service account
        return gspread.service_account(filename=str(CREDENTIALS))
    # OAuth flow (creates/reuses token.json)
    return gspread.oauth(
        credentials_filename=str(CREDENTIALS) if CREDENTIALS.exists() else None,
        authorized_user_filename=str(TOKEN),
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def header_format():
    return CellFormat(
        backgroundColor=HEADER_BG,
        textFormat=TextFormat(bold=True, foregroundColor=HEADER_TEXT),
    )


def row_format(bucket):
    color = BUCKET_COLORS.get(bucket, Color(1, 1, 1))
    return CellFormat(backgroundColor=color)


def money_format():
    return CellFormat(numberFormat={"type": "CURRENCY", "pattern": "$#,##0.00"})


def date_fmt():
    return CellFormat(numberFormat={"type": "DATE", "pattern": "yyyy-mm-dd"})


def col_letter(n):
    """Convert 1-indexed column number to letter (1=A, 27=AA, etc.)."""
    result = ""
    while n:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result


# ---------------------------------------------------------------------------
# Write Aging Detail tab
# ---------------------------------------------------------------------------

def write_detail_tab(spreadsheet, invoices):
    try:
        ws = spreadsheet.worksheet("Aging Detail")
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet("Aging Detail", rows=1000, cols=len(DETAIL_HEADERS))

    rows = [DETAIL_HEADERS]
    for inv in invoices:
        rows.append([
            inv.get("invoice_number", ""),
            inv.get("po_number", ""),
            inv.get("retailer", ""),
            inv.get("issue_date", ""),
            inv.get("due_date", ""),
            inv.get("amount_total", 0),
            inv.get("amount_paid", 0),
            inv.get("amount_outstanding", 0),
            inv.get("days_overdue", 0),
            inv.get("bucket", ""),
        ])

    ws.update("A1", rows)

    # Format header row
    last_col = col_letter(len(DETAIL_HEADERS))
    format_cell_range(ws, f"A1:{last_col}1", header_format())
    set_frozen(ws, rows=1)

    # Color-code data rows by bucket
    for i, inv in enumerate(invoices, start=2):
        bucket = inv.get("bucket", "current")
        fmt = row_format(bucket)
        format_cell_range(ws, f"A{i}:{last_col}{i}", fmt)

    print(f"  'Aging Detail' tab: {len(invoices)} rows written")
    return ws


# ---------------------------------------------------------------------------
# Write Summary tab
# ---------------------------------------------------------------------------

def write_summary_tab(spreadsheet, invoices):
    try:
        ws = spreadsheet.worksheet("Summary")
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet("Summary", rows=20, cols=3)

    today = date.today().isoformat()
    summary_header = [["Outstanding Invoice Aging Report", f"As of {today}", ""]]
    blank = [["", "", ""]]
    col_headers = [["Aging Bucket", "Invoice Count", "Total Outstanding ($)"]]

    bucket_rows = []
    for bucket in AGING_BUCKETS:
        bucket_invoices = [i for i in invoices if i.get("bucket") == bucket]
        count = len(bucket_invoices)
        total = round(sum(i.get("amount_outstanding", 0) for i in bucket_invoices), 2)
        bucket_rows.append([bucket, count, total])

    grand_count = len(invoices)
    grand_total = round(sum(i.get("amount_outstanding", 0) for i in invoices), 2)
    grand_row = [["TOTAL", grand_count, grand_total]]

    all_rows = summary_header + blank + col_headers + bucket_rows + blank + grand_row
    ws.update("A1", all_rows)

    # Format title
    format_cell_range(ws, "A1:C1", CellFormat(
        textFormat=TextFormat(bold=True, fontSize=12),
    ))
    # Format column headers
    format_cell_range(ws, "A3:C3", header_format())
    # Format grand total row
    grand_row_num = 3 + 1 + len(AGING_BUCKETS) + 2
    format_cell_range(ws, f"A{grand_row_num}:C{grand_row_num}", CellFormat(
        textFormat=TextFormat(bold=True),
    ))

    print(f"  'Summary' tab: {len(AGING_BUCKETS)} buckets + grand total written")
    return ws


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not INPUT_PATH.exists():
        print(f"ERROR: {INPUT_PATH} not found. Run tools/crstl_fetch_invoices.py first.")
        sys.exit(1)

    if not SHEETS_URL:
        print("ERROR: GOOGLE_SHEETS_URL not set in .env")
        sys.exit(1)

    invoices = json.loads(INPUT_PATH.read_text())
    print(f"Loaded {len(invoices)} outstanding invoices from {INPUT_PATH}")

    # Summary to stdout
    print()
    print("Aging breakdown:")
    for bucket in AGING_BUCKETS:
        b_invoices = [i for i in invoices if i.get("bucket") == bucket]
        total = sum(i.get("amount_outstanding", 0) for i in b_invoices)
        print(f"  {bucket:<10} {len(b_invoices):>4} invoices   ${total:>12,.2f}")
    grand = sum(i.get("amount_outstanding", 0) for i in invoices)
    print(f"  {'TOTAL':<10} {len(invoices):>4} invoices   ${grand:>12,.2f}")
    print()

    # Connect to Google Sheets
    print("Connecting to Google Sheets...")
    try:
        gc = get_client()
        spreadsheet = gc.open_by_url(SHEETS_URL)
        print(f"  Opened: {spreadsheet.title}")
    except Exception as e:
        print(f"  ERROR connecting to Google Sheets: {e}")
        print("  Make sure credentials.json is present and GOOGLE_SHEETS_URL is correct.")
        sys.exit(1)

    # Write tabs
    print("Writing tabs...")
    write_detail_tab(spreadsheet, invoices)
    write_summary_tab(spreadsheet, invoices)

    print(f"\nDone. Open your sheet: {SHEETS_URL}")


if __name__ == "__main__":
    main()
