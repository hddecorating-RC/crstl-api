"""
push_invoices_to_netsuite.py
----------------------------
Push Crstl invoices into NetSuite as INVOICE records, via the TBA connector
(app/netsuite_client.py). Accounting books this Home Depot revenue as invoices.

Chain:
    .tmp/invoices_raw.json           (tools/crstl_fetch_invoices.py)
      -> province/store attached      (app.main._attach_provinces, upstream)
      -> transform_invoice(...)        (app/netsuite.py -- business mapping)
      -> build_invoice_payload(...)    (app/netsuite_payload.py -- REST body, id refs)
      -> NetSuiteClient.upsert_invoice (app/netsuite_client.py -- TBA transport)

Internal ids for item and tax code come from config/netsuite_customers.json
(item_ids / tax_code_ids). Any still blank are reported here and BLOCK a --live
send -- fill them from NetSuite first (see tools/netsuite_probe.py).

Usage:
    python tools/push_invoices_to_netsuite.py            # dry run: build + print, send nothing
    python tools/push_invoices_to_netsuite.py --live     # upsert (needs creds + filled ids)
    python tools/push_invoices_to_netsuite.py --only TXN  # just one invoice by transaction_id
    python tools/push_invoices_to_netsuite.py --limit 1   # cap how many are processed

There is no sandbox: use --only/--limit to send a single controlled record first.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import load_env  # noqa: E402  -- also loads .env at import
from app.netsuite import transform_invoice  # noqa: E402
from app.netsuite_payload import build_invoice_payload, load_refs, unresolved_ids  # noqa: E402


def _load_invoices() -> list[dict]:
    """Build invoices the way the app does: fetch transaction details from Crstl,
    flatten them (crstl._extract_invoice_fields), and attach province/store from
    the source PO (main._attach_provinces). Read-only on Crstl; needs
    CRSTL_API_KEY. This is the same pipeline the dashboard/report uses -- the raw
    .tmp file is nested {file, metadata} and is NOT the flat shape we need."""
    from app.crstl import CrstlClient
    from app.main import _attach_provinces
    client = CrstlClient()
    print("Fetching invoices from Crstl (read-only)...")
    invoices = client.fetch_invoices()
    _attach_provinces(invoices, client.fetch_po_provinces())
    if not invoices:
        print("No invoices to push.")
        sys.exit(0)
    print(f"  {len(invoices)} invoices fetched.")
    return invoices


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="actually upsert to NetSuite (needs NETSUITE_* creds + filled ids)")
    parser.add_argument("--only", metavar="TXN_ID", help="push just this transaction_id")
    parser.add_argument("--limit", type=int, help="cap the number processed")
    args = parser.parse_args()

    load_env()
    invoices = _load_invoices()
    if args.only:
        invoices = [i for i in invoices if str(i.get("transaction_id")) == args.only]
        if not invoices:
            print(f"No invoice with transaction_id={args.only}.")
            sys.exit(1)
    if args.limit:
        invoices = invoices[: args.limit]

    refs = load_refs()

    # Map everything first, so id gaps are reported before NetSuite is touched.
    prepared: list[tuple[dict, dict]] = []
    skipped_no_map = 0
    all_unresolved: set[str] = set()
    for inv in invoices:
        lines = transform_invoice(inv, inv.get("province"), inv.get("store"))
        if not lines:
            skipped_no_map += 1
            print(f"  skip  {inv.get('transaction_id', '?')}: no province/store mapping")
            continue
        all_unresolved.update(unresolved_ids(lines, refs))
        prepared.append((inv, build_invoice_payload(lines, refs)))

    if all_unresolved:
        print("\nMissing internal ids in config/netsuite_customers.json "
              "(fill these before a live send):")
        for tag in sorted(all_unresolved):
            print(f"    {tag}")

    client = None
    if args.live:
        if all_unresolved:
            print("\nERROR: cannot --live while ids are unresolved. Fill them in and re-run.")
            sys.exit(1)
        from app.netsuite_client import NetSuiteClient, NetSuiteUnavailable
        if not NetSuiteClient.configured():
            print("ERROR: --live needs NETSUITE_* credentials (see .env.example).")
            sys.exit(1)
        try:
            client = NetSuiteClient()
            client.test_connection()
        except NetSuiteUnavailable as exc:
            print(f"ERROR: NetSuite not reachable: {exc}")
            sys.exit(1)
        print("NetSuite connection OK.")

    sent = failed = 0
    for inv, body in prepared:
        eid = inv.get("transaction_id", "?")
        if not args.live:
            print(f"  DRY   {eid}: would PUT invoice/eid:{body['externalId']} "
                  f"({len(body['item']['items'])} line(s))")
            print(json.dumps(body, indent=2, default=str))
            sent += 1
            continue
        try:
            result = client.upsert_invoice(body)
            sent += 1
            print(f"  sent  {eid}: {result.get('location') or result}")
        except Exception as exc:  # one bad invoice shouldn't stop the batch
            failed += 1
            print(f"  FAIL  {eid}: {exc}")

    mode = "LIVE" if args.live else "DRY RUN"
    print(f"\n{mode}: {sent} built/sent, {skipped_no_map} skipped (no mapping), {failed} failed.")
    if not args.live:
        print("Fill any missing ids above, then re-run with --live "
              "(no sandbox -- use --only/--limit to send one controlled record first).")


if __name__ == "__main__":
    main()
