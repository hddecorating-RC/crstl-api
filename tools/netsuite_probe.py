"""
netsuite_probe.py
-----------------
Read-only NetSuite explorer. Makes NO writes, so it is safe against production --
which matters because there is no sandbox. Two purposes:

  1. Get TBA working: confirm the credentials and OAuth signing (a GET/SELECT,
     creates nothing).
  2. Look up the internal ids the connector needs, so we fill
     config/netsuite_customers.json ourselves instead of waiting on accounting.

Usage:
    python tools/netsuite_probe.py                       # test connection; list recent salesOrder ids
    python tools/netsuite_probe.py --id 123456           # GET salesOrder 123456 (read its item/tax/class ids)
    python tools/netsuite_probe.py --type invoice --id 1 # GET another record type
    python tools/netsuite_probe.py --sql "SELECT id, itemid, displayname FROM item"
    python tools/netsuite_probe.py --sql "SELECT id, itemid FROM item WHERE itemid IN ('Merchandise Sales','Allowance','Charge')"
"""
import argparse
import json
import sys

from app.netsuite_client import NetSuiteClient, NetSuiteUnavailable


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", help="internal id of a record to fetch (read-only)")
    parser.add_argument("--type", default="salesOrder",
                        help="record type for --id (default: salesOrder)")
    parser.add_argument("--sql", help="run a read-only SuiteQL SELECT and print the rows")
    args = parser.parse_args()

    if not NetSuiteClient.configured():
        print("ERROR: NETSUITE_* credentials not set (see .env.example).")
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

        print("Testing connection (GET salesOrder?limit=5)...")
        result = client.list_records("salesOrder", limit=5)
        ids = [item.get("id") for item in result.get("items", [])]
        print(f"  OK -- credentials and signing work. Recent salesOrder ids: {ids}")
        print("  Next: python tools/netsuite_probe.py --id <one of those>  "
              "to read its item/tax/class ids,")
        print("  or:   python tools/netsuite_probe.py --sql \"SELECT id, itemid FROM item ...\"")
    except NetSuiteUnavailable as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
