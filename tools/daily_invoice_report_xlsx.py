"""Daily invoice report for accounting, as a .xlsx workbook.

One row per invoice, showing subtotal, every deduction, tax by kind, and the
invoice total -- with a reconciliation column proving each row ties back to the
total HD stated. That reconciliation is the point: a row that does not balance
is an invoice worth looking at before it is keyed.

Reads Crstl directly and parses the raw 810 payload, so it does not depend on
which version of the app is deployed. Accepted invoices only -- Drafts are the
warehouse's superseded resubmissions and would duplicate rows.

Tax reaches us by two different routes depending on the flavor, and the report
has to read both:

    Dropship  tax_information (TXI)  CG=GST  ST=QST  VA=HST
    DSD       SAC codes              D360=GST  H680=QST  H770=HST  H850=ECO

Dropship invoices carry no ship-to, so the province is recovered from the 850
PO. That province picks the NetSuite customer and tax code; DSD picks them from
the store on the PO instead.

Usage:
    python3 tools/daily_invoice_report_xlsx.py                  # yesterday
    python3 tools/daily_invoice_report_xlsx.py --date 2026-08-24
    python3 tools/daily_invoice_report_xlsx.py --from 2026-08-01 --to 2026-08-24
    python3 tools/daily_invoice_report_xlsx.py --out /path/to/file.xlsx
"""
import argparse
import json
import os
import pathlib
import sys
from datetime import date, timedelta

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# Tax vocabularies. Crstl spells CG out as "Federal Value Added Tax GST On
# Goods" in its own UI, which is where these readings come from -- not from
# matching amounts against province rates.
TXI_KIND = {"CG": "GST", "ST": "QST", "VA": "HST"}
SAC_KIND = {"D360": "GST", "H680": "QST", "H770": "HST",
            "H850": "ECO", "F240": "ECO", "G090": "ECO", "G100": "ECO"}

MONEY = "#,##0.00"
HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(bold=True, color="FFFFFF", size=10)
BAD_FILL = PatternFill("solid", fgColor="FFC7CE")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _num(x):
    try:
        return round(float(x or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _load_env():
    env = pathlib.Path(REPO) / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def extract(detail, po_index, config):
    """Flatten one 810 payload into the row accounting needs."""
    meta = detail.get("metadata", {}) or {}
    edi = (detail.get("file", {}) or {}).get("generic_json_edi", {}) or {}
    heading = edi.get("heading", {}) or {}
    summary = edi.get("summary", {}) or {}

    flavor = "DSD" if "DSD" in (meta.get("trading_partner_flavor") or "") else "Dropship"
    po = str(meta.get("source_document_reference_id") or heading.get("purchase_order_number") or "")

    # DSD carries its own ship-to; Dropship does not, so fall back to the PO.
    ship_to = heading.get("ship_to") or {}
    po_info = po_index.get(po, {})
    province = (str(ship_to.get("state_province") or "").upper()
                or str(po_info.get("province") or "").upper())
    store = po_info.get("store")
    if not store and ship_to.get("name"):
        name = str(ship_to["name"]).upper()
        store = "VAUGHAN" if "VAUGHAN" in name else "CALGARY" if "CALGARY" in name else None

    mapping = (config["wholesale_stores"].get(store or "", {}) if flavor == "DSD"
               else config["dropship_provinces"].get(province, {}))

    lines = []
    for entry in (edi.get("detail", {}) or {}).get("baseline_item_data_invoice_loop", []) or []:
        base = entry.get("baseline_item_data_invoice", {}) or {}
        lines.append(_num(base.get("quantity_invoiced")) * _num(base.get("unit_price")))
    subtotal = round(sum(lines), 2)

    deductions, charges, taxes = {}, {}, {}
    for entry in summary.get("service_promotion_allowance_or_charge_information_loop", []) or []:
        sac = entry.get("service_promotion_allowance_or_charge_information", {}) or {}
        code = sac.get("service_promotion_allowance_or_charge_code") or "?"
        amt = _num(sac.get("amount"))
        if code in SAC_KIND:
            taxes[SAC_KIND[code]] = round(taxes.get(SAC_KIND[code], 0) + amt, 2)
        elif sac.get("allowance_or_charge_indicator") == "A":
            deductions[code] = round(deductions.get(code, 0) + amt, 2)
        else:
            charges[code] = round(charges.get(code, 0) + amt, 2)

    for entry in summary.get("tax_information", []) or []:
        kind = TXI_KIND.get(entry.get("tax_type_code"), entry.get("tax_type_code") or "?")
        taxes[kind] = round(taxes.get(kind, 0) + _num(entry.get("monetary_amount")), 2)

    stated = _num((summary.get("total_monetary_value_summary", {}) or {}).get("total_amount")
                  or meta.get("value"))
    computed = round(subtotal - sum(deductions.values()) + sum(charges.values())
                     + sum(taxes.values()), 2)

    return {
        "invoice": heading.get("invoice_number") or meta.get("reference_id") or "",
        "date": heading.get("invoice_date") or "",
        "po": po,
        "flavor": flavor,
        "province": province,
        "store": store or "",
        "customer": mapping.get("customer_id", ""),
        "tax_code": mapping.get("tax_code", ""),
        "currency": (heading.get("currency") or {}).get("currency_code") or config.get("currency", "CAD"),
        "line_count": len(lines),
        "subtotal": subtotal,
        "deductions": deductions,
        "charges": charges,
        "taxes": taxes,
        "stated": stated,
        "computed": computed,
        "variance": round(stated - computed, 2),
    }


def build_workbook(rows, out_path, window):
    """Invoices sheet: what was invoiced and how much, one row each.

    Deliberately plain -- invoice, province, date, subtotal, discounts, tax,
    total. The per-code and per-kind breakdowns live in the payload and can be
    added back, but accounting asked for the money, not the mechanics.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Invoices"

    headers = ["Invoice", "Type", "Province", "Invoice Date", "Subtotal",
               "Discounts", "Tax", "Total"]
    ws.append(headers)
    for cell in ws[1]:
        cell.fill, cell.font = HDR_FILL, HDR_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for r in rows:
        ws.append([r["invoice"], r["flavor"], r["province"], r["date"], r["subtotal"],
                   -round(sum(r["deductions"].values()), 2) or 0,
                   round(sum(r["taxes"].values()), 2), r["stated"]])

    last = ws.max_row + 1
    ws.append(["Total", "", "", "",
               round(sum(r["subtotal"] for r in rows), 2),
               -round(sum(sum(r["deductions"].values()) for r in rows), 2),
               round(sum(sum(r["taxes"].values()) for r in rows), 2),
               round(sum(r["stated"] for r in rows), 2)])
    for cell in ws[last]:
        cell.font = Font(bold=True)
        cell.border = Border(top=Side(style="thin"))

    for row in ws.iter_rows(min_row=2, min_col=5):
        for cell in row:
            if isinstance(cell.value, (int, float)):
                cell.number_format = MONEY
    for i, w in enumerate((18, 11, 10, 14, 13, 12, 12, 13), start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:H{last - 1}"

    # Summary: the same money by flavor, plus anything that does not tie back
    # to HD's stated total. Kept off the main sheet so the report stays plain.
    s = wb.create_sheet("Summary")
    s.append(["Invoiced", window]); s["A1"].font = Font(bold=True, size=13)
    s.append(["Source", "Crstl 810 transactions, state = Accepted"])
    s.append([])
    s.append(["Type", "Invoices", "Subtotal", "Discounts", "Tax", "Total"])
    for cell in s[4]:
        cell.fill, cell.font = HDR_FILL, HDR_FONT
    for flavor in ("Dropship", "DSD"):
        sub = [r for r in rows if r["flavor"] == flavor]
        if not sub:
            continue
        s.append([flavor, len(sub),
                  round(sum(r["subtotal"] for r in sub), 2),
                  -round(sum(sum(r["deductions"].values()) for r in sub), 2),
                  round(sum(sum(r["taxes"].values()) for r in sub), 2),
                  round(sum(r["stated"] for r in sub), 2)])
    s.append(["Total", len(rows),
              round(sum(r["subtotal"] for r in rows), 2),
              -round(sum(sum(r["deductions"].values()) for r in rows), 2),
              round(sum(sum(r["taxes"].values()) for r in rows), 2),
              round(sum(r["stated"] for r in rows), 2)])
    for cell in s[s.max_row]:
        cell.font = Font(bold=True)

    unbalanced = [r for r in rows if abs(r["variance"]) >= 0.005]
    s.append([])
    s.append(["Do not match HD's total", len(unbalanced)])
    s[s.max_row][0].font = Font(bold=True)
    if unbalanced:
        s[s.max_row][1].fill = BAD_FILL
        s.append(["Invoice", "HD total", "Adds up to", "Difference"])
        for cell in s[s.max_row]:
            cell.font = Font(bold=True)
        for r in unbalanced:
            s.append([r["invoice"], r["stated"], r["computed"], r["variance"]])
    for row in s.iter_rows(min_row=5):
        for cell in row:
            # column 2 on the flavor rows is a count of invoices, not money
            if isinstance(cell.value, (int, float)) and cell.column > 2:
                cell.number_format = MONEY
    for i, w in enumerate((24, 14, 14, 14, 12, 14), start=1):
        s.column_dimensions[get_column_letter(i)].width = w

    wb.save(out_path)
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="single invoice_date (YYYY-MM-DD); defaults to yesterday")
    ap.add_argument("--from", dest="date_from", help="start of an inclusive range")
    ap.add_argument("--to", dest="date_to", help="end of an inclusive range")
    ap.add_argument("--out", help="output .xlsx path")
    args = ap.parse_args()

    if args.date_from or args.date_to:
        lo = args.date_from or "0000-00-00"
        hi = args.date_to or "9999-99-99"
    else:
        day = args.date or (date.today() - timedelta(days=1)).isoformat()
        lo = hi = day
    window = lo if lo == hi else f"{lo} to {hi}"

    _load_env()
    from app.crstl import CrstlClient
    client = CrstlClient(base_url=os.environ.get("CRSTL_BASE_URL", "https://api.crstl.so/v2"),
                         api_key=os.environ["CRSTL_API_KEY"])
    config = json.loads((pathlib.Path(REPO) / "config" / "netsuite_customers.json").read_text())

    print(f"Fetching 810 transactions for {window} ...")
    accepted = [t for t in client._fetch_all_transactions("810")
                if (t.get("state") or {}).get("value") == "Accepted"]
    po_index = client.fetch_po_provinces()

    rows = []
    for t in accepted:
        detail = client._fetch_transaction_detail(t["id"])
        row = extract(detail, po_index, config)
        if lo <= (row["date"] or "") <= hi:
            rows.append(row)
    rows.sort(key=lambda r: (r["flavor"], r["invoice"]))

    if not rows:
        print(f"No Accepted invoices dated {window}.")
        return 1

    out = args.out or os.path.join(REPO, ".tmp", f"HD_Invoices_{lo}.xlsx" if lo == hi
                                   else f"HD_Invoices_{lo}_to_{hi}.xlsx")
    pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
    build_workbook(rows, out, window)
    bad = sum(1 for r in rows if abs(r["variance"]) >= 0.005)
    print(f"{len(rows)} invoices ({sum(1 for r in rows if r['flavor']=='Dropship')} Dropship, "
          f"{sum(1 for r in rows if r['flavor']=='DSD')} DSD)")
    print(f"{bad} do not reconcile" if bad else "all invoices reconcile")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
