"""Daily invoice report for accounting, as a .xlsx workbook.

One row per invoice -- invoice, type, province, date, subtotal, discounts,
charges, tax, total -- plus a total row. The Summary sheet carries the same
money split by type and lists anything whose parts do not add up to the total
HD stated, because an invoice that does not balance is worth seeing before it
is keyed rather than after.

The workbook itself is built by app/report.py, which the dashboard's Export
button and the digest email attachment also use -- one definition of the
columns, so a change here reaches accounting by every route at once. This file
is the date-windowed CLI over it.

Reads the raw 810 payload from Crstl rather than going through the app cache,
so it runs regardless of which version is deployed. Accepted only: Drafts are
the warehouse's superseded resubmissions and would duplicate rows.

Usage:
    python3 tools/daily_invoice_report_xlsx.py                  # yesterday
    python3 tools/daily_invoice_report_xlsx.py --date 2026-08-24
    python3 tools/daily_invoice_report_xlsx.py --from 2026-08-01 --to 2026-08-24
    python3 tools/daily_invoice_report_xlsx.py --out /path/to/file.xlsx
"""
import argparse
import os
import pathlib
import sys
from datetime import date, datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from app.crstl import CrstlClient                                    # noqa: E402
from app.main import load_env                                        # noqa: E402
from app.report import (extract, fetch_details, reconciles,          # noqa: E402
                        save_workbook)


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

    load_env(os.path.join(REPO, ".env"))
    client = CrstlClient(base_url=os.environ.get("CRSTL_BASE_URL", "https://api.crstl.so/v2"),
                         api_key=os.environ["CRSTL_API_KEY"])

    print(f"Fetching 810 transactions for {window} ...")
    listing = client._fetch_all_transactions("810")

    accepted, unknown_states = [], {}
    for t in listing:
        state = (t.get("state") or {}).get("value")
        if state == "Accepted":
            accepted.append(t)
        elif state != "Draft":
            unknown_states[state] = unknown_states.get(state, 0) + 1
    if unknown_states:
        # Same reasoning as app/main.py's _reportable: an unrecognised state
        # must not vanish silently, or a new "good" state would quietly stop
        # reaching accounting.
        print(f"WARNING: withheld invoices with unrecognised status {unknown_states} — "
              f"if one of these is reportable, this filter needs updating")

    # Narrow by created_at before fetching details. A generous buffer keeps
    # invoices whose invoice_date trails their creation; the exact filter on
    # invoice_date still happens after extraction.
    def created(t):
        return str(t.get("created_at") or "")[:10]
    buf_lo = (datetime.fromisoformat(lo).date() - timedelta(days=14)).isoformat() if lo[0].isdigit() and lo != "0000-00-00" else lo
    buf_hi = (datetime.fromisoformat(hi).date() + timedelta(days=14)).isoformat() if hi[0].isdigit() and hi != "9999-99-99" else hi
    candidates = [t for t in accepted if not created(t) or buf_lo <= created(t) <= buf_hi]

    details = fetch_details(client, candidates)

    # Only look up the POs actually needed; fetch_po_provinces() crawls every
    # 850 on record, which is wasted work for a one-day report.
    need_po = {str((d.get("metadata") or {}).get("source_document_reference_id") or "")
               for d in details}
    po_index = {}
    pos = [p for p in client._fetch_all_transactions("850")
           if str(p.get("reference_id") or "") in need_po]
    for detail in fetch_details(client, pos):
        st = ((detail.get("file", {}) or {}).get("generic_json_edi", {})
              .get("heading", {}) or {}).get("ship_to", {}) or {}
        ref = str((detail.get("metadata") or {}).get("reference_id") or "")
        if ref and st.get("state_province"):
            po_index[ref] = {"province": str(st["state_province"]).upper()}

    rows, undated = [], 0
    for detail in details:
        row = extract(detail, po_index)
        if not row["date"]:
            undated += 1
            print(f"WARNING: {row['invoice']} has no invoice_date and is not in any window")
            continue
        if lo <= row["date"] <= hi:
            rows.append(row)
    rows.sort(key=lambda r: (r["flavor"], r["invoice"]))

    if not rows:
        # A quiet day is not a failure. A cron wrapper must not read it as one.
        print(f"No Accepted invoices dated {window}.")
        return 0

    out = args.out or os.path.join(REPO, ".tmp",
                                   f"HD_Invoices_{lo}.xlsx" if lo == hi
                                   else f"HD_Invoices_{lo}_to_{hi}.xlsx")
    pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
    save_workbook(rows, out, window)

    bad = sum(1 for r in rows if not reconciles(r))
    by_flavor = ", ".join(f"{sum(1 for r in rows if r['flavor'] == f)} {f}"
                          for f in sorted({r["flavor"] for r in rows}))
    print(f"{len(rows)} invoices ({by_flavor})")
    print(f"{bad} do not reconcile" if bad else "all invoices reconcile")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
