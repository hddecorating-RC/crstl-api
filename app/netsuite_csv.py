import csv
import io

COLUMNS = [
    "External ID",
    "Customer",
    "Date",
    "Due Date",
    "Memo",
    "PO Number",
    "Item",
    "Description",
    "Quantity",
    "Rate",
    "Tax Code",
    "Tax Amount",
    "Currency",
]


def build_netsuite_csv(records: list[dict]) -> bytes:
    """Render NetSuite invoice import CSV from transformed records.

    Each record is one line item. Records sharing an `external_id` are treated
    by NetSuite as lines on the same invoice.
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS)
    writer.writeheader()
    for r in records:
        writer.writerow({
            "External ID": r["external_id"],
            "Customer":    r["customer_id"],
            "Date":        r["tran_date"],
            "Due Date":    r["due_date"],
            "Memo":        r["memo"],
            "PO Number":   r["other_ref_num"],
            "Item":        r["item"],
            "Description": r.get("description", ""),
            "Quantity":    r["quantity"],
            "Rate":        r["rate"],
            "Tax Code":    r["tax_code"],
            "Tax Amount":  r["tax_amount"],
            "Currency":    r["currency"],
        })
    return buf.getvalue().encode("utf-8")
