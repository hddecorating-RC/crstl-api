import csv
import io

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


def build_csv(invoices: list[dict], ids: list[str] | None = None) -> bytes:
    """Return CSV bytes for invoices. Pass ids to export a subset by transaction_id."""
    if ids is not None:
        invoices = [inv for inv in invoices if inv.get("transaction_id") in ids]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=[col for col, _ in COLUMNS])
    writer.writeheader()
    for inv in invoices:
        writer.writerow({col: inv.get(key, "") for col, key in COLUMNS})
    return buf.getvalue().encode("utf-8")
