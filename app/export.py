import csv
import io

from app.sac_codes import label as sac_label

BASE_COLUMNS = [
    ("Invoice Number",   "invoice_number"),
    ("PO Number",        "po_number"),
    ("Trading Partner",  "trading_partner"),
    ("Ship-to Province", "province"),
    ("Invoice Date",     "invoice_date"),
    ("Due Date",         "due_date"),
    ("Status",           "status"),
    ("Subtotal",         "subtotal"),
    # Per-category summary totals. Each cell = sum of the SAC codes classified
    # in that bucket (see app/sac_codes.py). "Charge Total" is retained for
    # anyone still opening the old CSV shape; it equals Freight + Fee + Tax.
    ("Allowance Total",  "allowance_amount"),
    ("Discount Total",   "discount_amount"),
    ("Freight Total",    "freight_amount"),
    ("Fee Total",        "fee_amount"),
]

TAIL_COLUMNS = [
    ("Tax Amount",     "tax_amount"),
    ("Tax GST",        "_tax_gst"),
    ("Tax HST/QST",    "_tax_hst_qst"),
    ("Tax Eco",        "_tax_eco"),
    ("Charge Total",   "charge_amount"),  # legacy = freight+fee+tax
    ("Total Amount",   "total_amount"),
    # Discrepancy = total − (subtotal − allowance − discount + freight + fee + tax).
    # Zero on a clean invoice. Non-zero flags a data quality issue in HD's SAC —
    # use this column to filter/sort for reconciliation review.
    ("Discrepancy",    "discrepancy"),
    ("Currency",       "currency"),
    ("Transaction ID", "transaction_id"),
    ("Created At",     "created_at"),
]


def _collect_codes(invoices: list[dict]) -> list[str]:
    """Return the sorted union of SAC codes across all invoices. A code may appear
    as an Allowance on one invoice and a Charge on another (rare); a single column
    carries a signed amount either way, so no duplication."""
    codes: set[str] = set()
    for inv in invoices:
        for entry in inv.get("allowances_charges") or []:
            if entry.get("code"):
                codes.add(entry["code"])
    return sorted(codes)


def build_csv(invoices: list[dict], ids: list[str] | None = None) -> bytes:
    """Return CSV bytes for invoices. Pass ids to export a subset by transaction_id.

    A dedicated column is emitted for every SAC code present in the dataset:
    allowances are shown as negative values, charges as positive. Codes not
    present on a given invoice are left blank so `SUM()` in Excel ignores them.
    """
    if ids is not None:
        invoices = [inv for inv in invoices if inv.get("transaction_id") in ids]

    codes = _collect_codes(invoices)
    code_columns = [(sac_label(c), c) for c in codes]
    fieldnames = [name for name, _ in BASE_COLUMNS] + [name for name, _ in code_columns] + [name for name, _ in TAIL_COLUMNS]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for inv in invoices:
        row = {name: inv.get(key, "") for name, key in BASE_COLUMNS}
        row.update({name: inv.get(key, "") for name, key in TAIL_COLUMNS})
        # Split the tax breakdown dict into its own columns so accounting can
        # pivot by tax kind without post-processing the CSV.
        tb = inv.get("tax_breakdown") or {}
        row["Tax GST"]     = tb.get("GST", "")
        row["Tax HST/QST"] = tb.get("HST_QST", "")
        row["Tax Eco"]     = tb.get("ECO", "")
        code_amounts: dict[str, float] = {}
        for entry in inv.get("allowances_charges") or []:
            code = entry.get("code")
            if not code:
                continue
            amt = entry.get("amount", 0)
            signed = -amt if entry.get("type") == "Allowance" else amt
            code_amounts[code] = round(code_amounts.get(code, 0) + signed, 2)
        for name, code in code_columns:
            row[name] = code_amounts.get(code, "")
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")
