"""
push_invoices_to_netsuite.py
----------------------------
Push Crstl invoices into NetSuite as invoice records, via the TBA connector
(app/netsuite_client.py). This is the automated replacement for the manual CSV
import (tools/generate_accounting_csv.py).

The whole chain is:
    .tmp/invoices_raw.json           (tools/crstl_fetch_invoices.py)
      -> province/store attached     (app.main._attach_provinces, upstream)
      -> transform_invoice(...)       (app/netsuite.py  -- the business mapping)
      -> build_invoice_payload(...)   (app/netsuite_payload.py -- REST body)
      -> NetSuiteClient.upsert_invoice (app/netsuite_client.py -- TBA transport)

Usage:
    python tools/push_invoices_to_netsuite.py            # dry run: build + print, send nothing
    python tools/push_invoices_to_netsuite.py --live     # actually upsert (needs NETSUITE_* creds)

--dry-run (the default) needs NO credentials: it runs the mapping and prints the
exact NetSuite bodies that WOULD be sent, so the whole pipeline is verifiable
today. --live builds a NetSuiteClient from the environment and upserts each
invoice; run it against a NetSuite SANDBOX account first.

Note: this reads province/store off each invoice. Enrich the raw invoices first
(app.main._attach_provinces, or the dashboard path); invoices with no mapping are
reported and skipped rather than guessed.
"""
import argparse
import json
import pathlib
import sys

from app.netsuite import transform_invoice
from app.netsuite_payload import build_invoice_payload

INPUT_PATH = pathlib.Path(".tmp/invoices_raw.json")


def _load_invoices() -> list[dict]:
    if not INPUT_PATH.exists():
        print(f"ERROR: {INPUT_PATH} not found. Run tools/crstl_fetch_invoices.py first.")
        sys.exit(1)
    invoices = json.loads(INPUT_PATH.read_text())
    if not invoices:
        print("No invoices to push.")
        sys.exit(0)
    return invoices


def _payload_for(invoice: dict):
    """Map one invoice to a NetSuite body, or None if it has no province/store
    mapping (which transform_invoice signals by returning None)."""
    lines = transform_invoice(invoice, invoice.get("province"), invoice.get("store"))
    if not lines:
        return None
    return build_invoice_payload(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="actually upsert to NetSuite (needs NETSUITE_* credentials)")
    args = parser.parse_args()

    invoices = _load_invoices()

    client = None
    if args.live:
        from app.netsuite_client import NetSuiteClient, NetSuiteUnavailable
        if not NetSuiteClient.configured():
            print("ERROR: --live needs NETSUITE_* credentials (see .env.example). Aborting.")
            sys.exit(1)
        try:
            client = NetSuiteClient()
            client.test_connection()
        except NetSuiteUnavailable as exc:
            print(f"ERROR: NetSuite not reachable: {exc}")
            sys.exit(1)
        print("NetSuite connection OK.")

    sent = skipped = failed = 0
    for invoice in invoices:
        eid = invoice.get("transaction_id", "?")
        body = _payload_for(invoice)
        if body is None:
            skipped += 1
            print(f"  skip  {eid}: no province/store mapping")
            continue

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
        except Exception as exc:  # keep going; one bad invoice shouldn't stop the run
            failed += 1
            print(f"  FAIL  {eid}: {exc}")

    mode = "LIVE" if args.live else "DRY RUN"
    print(f"\n{mode}: {sent} built/sent, {skipped} skipped (no mapping), {failed} failed.")
    if not args.live:
        print("Re-run with --live once NETSUITE_* credentials are set (sandbox first).")


if __name__ == "__main__":
    main()
