"""
netsuite_probe.py
-----------------
Read-only NetSuite explorer. Makes NO writes, so it is safe against production
(there is no sandbox). Two purposes:

  1. Get TBA working: confirm the credentials and OAuth signing (a read; creates
     nothing). The connection test fetches the invoice metadata schema, which
     does NOT require search/list permission.
  2. Look up the internal ids the connector needs, so we fill
     config/netsuite_customers.json ourselves.

Usage:
    python tools/netsuite_probe.py                    # test connection (invoice metadata)
    python tools/netsuite_probe.py --id 123456        # GET invoice 123456 by INTERNAL id (numeric)
    python tools/netsuite_probe.py --type salesOrder --id 123456
    python tools/netsuite_probe.py --sql "SELECT id, itemid, displayname FROM item WHERE itemid IN ('Merchandise Sales','Allowance','Charge')"

Note: --id takes the INTERNAL id (the number in the record URL, ...?id=NNNNN),
not the document number like INV12345.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import load_env  # noqa: E402  -- also loads .env at import
from app.netsuite_client import NetSuiteClient, NetSuiteUnavailable  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", help="INTERNAL id of a record to fetch (numeric, read-only)")
    parser.add_argument("--type", default="invoice",
                        help="record type for --id (default: invoice)")
    parser.add_argument("--sql", help="run a read-only SuiteQL SELECT and print the rows")
    args = parser.parse_args()

    load_env()
    if not NetSuiteClient.configured():
        print("ERROR: NETSUITE_* credentials not set in .env (see .env.example).")
        sys.exit(1)

    try:
        client = NetSuiteClient()

        if args.sql:
            print(f"SuiteQL: {args.sql}")
            result = client.suiteql(args.sql)
            print(json.dumps(result.get("items", result), indent=2, default=str))
            return

        if args.id:
            print(f"GET {args.type}/{args.id} (read-only):")
            print(json.dumps(client.get_record(args.type, args.id), indent=2, default=str))
            return

        print("Testing connection (invoice metadata, read-only)...")
        client.test_connection()
        print("  OK -- credentials, signing, and invoice access work.")
        print("  Next: --id <invoice internal id> to read a real record, "
              "or fill the ids in config/netsuite_customers.json.")
    except NetSuiteUnavailable as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
