"""
generate_accounting_csv.py
--------------------------
Reads .tmp/invoices_raw.json and writes a dated CSV for accounting to
import into NetSuite (or any accounting system).

Usage:
    python tools/generate_accounting_csv.py

Output:
    .tmp/invoices_YYYY-MM-DD.csv
"""

import csv
import json
import pathlib
import sys
from datetime import date

INPUT_PATH = pathlib.Path(".tmp/invoices_raw.json")
OUTPUT_DIR = pathlib.Path(".tmp")

COLUMNS = [
    ("Invoice Number",  "invoice_number"),
    ("PO Number",       "po_number"),
    ("Trading Partner", "trading_partner"),
    ("Invoice Date",    "invoice_date"),
    ("Due Date",        "due_date"),
    ("Status",          "status"),
    ("Subtotal",        "subtotal"),
    ("Tax Amount",      "tax_amount"),
    ("Total Amount",    "total_amount"),
    ("Currency",        "currency"),
    ("Transaction ID",  "transaction_id"),
    ("Created At",      "created_at"),
]


def main():
    if not INPUT_PATH.exists():
        print("ERROR: .tmp/invoices_raw.json not found.")
        print("Run python tools/crstl_fetch_invoices.py first.")
        sys.exit(1)

    invoices = json.loads(INPUT_PATH.read_text())
    if not invoices:
        print("No invoices in invoices_raw.json. Nothing to export.")
        sys.exit(0)

    output_path = OUTPUT_DIR / f"invoices_{date.today().isoformat()}.csv"

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[col for col, _ in COLUMNS])
        writer.writeheader()
        for inv in invoices:
            writer.writerow({col: inv.get(key, "") for col, key in COLUMNS})

    print(f"Wrote {len(invoices)} invoices to {output_path}")
    print()
    print("Summary:")
    total = sum(float(inv.get("total_amount") or 0) for inv in invoices)
    tax   = sum(float(inv.get("tax_amount") or 0) for inv in invoices)
    sub   = sum(float(inv.get("subtotal") or 0) for inv in invoices)

    statuses = {}
    for inv in invoices:
        s = inv.get("status") or "unknown"
        statuses[s] = statuses.get(s, 0) + 1

    print(f"  Invoices:  {len(invoices)}")
    print(f"  Subtotal:  ${sub:,.2f}")
    print(f"  Tax:       ${tax:,.2f}")
    print(f"  Total:     ${total:,.2f}")
    print(f"  Statuses:  {statuses}")
    print()
    print(f"Send {output_path} to accounting for NetSuite import.")


if __name__ == "__main__":
    main()
