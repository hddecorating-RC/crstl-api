"""
push_invoices_to_netsuite.py
----------------------------
Push Crstl invoices into NetSuite as INVOICE records via the TBA connector.
Thin CLI over app.netsuite_push.push_invoices -- the SAME engine the web app
(POST /api/netsuite) uses, so a CLI dry run and a dashboard dry run are identical.

Chain (inside the engine): transform_invoice -> build_invoice_payload ->
NetSuiteClient.upsert_invoice. Internal ids come from config/netsuite_customers.json;
any still blank are reported here and BLOCK a --live send.

Eligibility is enforced INSIDE push_invoices (app.netsuite_push.eligible_for_push):
only ACCEPTED invoices, one (latest) version per logical invoice, non-zero gross.
So --live from here can never book a Draft, a stale resubmission, or a $0 row --
the CLI gets exactly the same filter as the scheduled job and the dashboard.

Usage:
    python tools/push_invoices_to_netsuite.py            # dry run: build + reconcile, send nothing
    python tools/push_invoices_to_netsuite.py --live     # upsert (needs creds + filled ids)
    python tools/push_invoices_to_netsuite.py --only TXN  # just one invoice by transaction_id
    python tools/push_invoices_to_netsuite.py --limit 1   # cap how many are processed
    python tools/push_invoices_to_netsuite.py --json      # also dump the full result dict

There is no sandbox: use --only/--limit to send a single controlled record first.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import load_env  # noqa: E402  -- also loads .env at import
from app.netsuite_push import push_invoices  # noqa: E402


def _load_invoices() -> list[dict]:
    """Fetch invoices the way the app does (Crstl detail + province/store), then
    push. Read-only on Crstl; needs CRSTL_API_KEY."""
    from app.main import _attach_provinces, _get_client
    client = _get_client()
    print("Fetching invoices from Crstl (read-only)...")
    invoices = client.fetch_invoices()
    _attach_provinces(invoices, client.fetch_po_provinces())
    if not invoices:
        print("No invoices to push.")
        sys.exit(0)
    print(f"  {len(invoices)} invoices fetched.")
    return invoices


def _print_row(r: dict) -> None:
    if r["status"] == "skipped_no_map":
        print(f"  skip  {r['transaction_id']}: no province/store mapping")
        return
    chan = "DSD " if r["channel"] == "dsd" else "DROP"
    where = r.get("where") or "?"
    hd = r.get("hd_total")
    delta = r.get("delta")
    line = (f"  {r['status']:6} {r['transaction_id']} {chan} {where:<8} "
            f"gross {r['gross']:>10.2f}  disc {r['discount']:>9.2f}  net {r['net']:>10.2f}  "
            f"tax {r['tax']:>8.2f}  => total {r['total']:>10.2f}"
            f"   HD810 {('' if hd is None else format(hd, '.2f')):>10}"
            f"   d {('' if delta is None else format(delta, '+.2f')):>9}")
    if r["status"] == "sent":
        line += f"   -> {r.get('location')}"
    if r["status"] == "failed":
        line += f"   FAIL: {r.get('error')}"
    print(line)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="actually upsert to NetSuite (needs NETSUITE_* creds + filled ids)")
    parser.add_argument("--only", metavar="TXN_ID", help="push just this transaction_id")
    parser.add_argument("--limit", type=int, help="cap the number processed")
    parser.add_argument("--json", action="store_true", help="dump the full result dict")
    args = parser.parse_args()

    load_env()
    invoices = _load_invoices()

    if args.live:
        from app.netsuite_client import NetSuiteClient
        if not NetSuiteClient.configured():
            print("ERROR: --live needs NETSUITE_* credentials (see .env.example).")
            sys.exit(1)

    result = push_invoices(
        invoices,
        live=args.live,
        only=[args.only] if args.only else None,
        limit=args.limit,
    )

    if args.only and not result["results"]:
        print(f"No invoice with transaction_id={args.only}.")
        sys.exit(1)

    for r in result["results"]:
        _print_row(r)

    if result["unresolved"]:
        print("\nMissing internal ids in config/netsuite_customers.json "
              "(fill these before a live send):")
        for tag in sorted(result["unresolved"]):
            print(f"    {tag}")
        if args.live and result.get("blocked"):
            print("\nERROR: cannot --live while ids are unresolved. Fill them in and re-run.")

    if args.json:
        print("\n" + json.dumps(result, indent=2, default=str))

    s = result["summary"]
    mode = "LIVE" if args.live else "DRY RUN"
    print(f"\n{mode}: {s['built']} built, {s['sent']} sent, {s['failed']} failed, "
          f"{s['skipped_no_map']} skipped (no mapping).")
    if not args.live:
        print("Re-run with --live to send (no sandbox -- use --only/--limit for one controlled record).")


if __name__ == "__main__":
    main()
