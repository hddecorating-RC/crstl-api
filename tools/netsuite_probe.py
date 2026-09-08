"""
netsuite_probe.py
-----------------
Read-only NetSuite check. Makes NO writes, so it is safe against production --
which matters because there is no sandbox. Two jobs:

  1. Confirm the TBA credentials and OAuth signing work (a GET, creates nothing).
  2. Inspect the exact shape NetSuite accepts by fetching an existing record --
     e.g. a sales order OMIS already created -- so the connector's refs
     (item/taxCode/class ids) can be matched to a real, accepted example.

Usage:
    python tools/netsuite_probe.py                    # test connection only
    python tools/netsuite_probe.py --id 123456        # also GET salesOrder 123456 (internal id)
    python tools/netsuite_probe.py --type invoice --id 123456
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
    args = parser.parse_args()

    if not NetSuiteClient.configured():
        print("ERROR: NETSUITE_* credentials not set (see .env.example).")
        sys.exit(1)

    try:
        client = NetSuiteClient()
        print("Testing connection (GET salesOrder?limit=1)...")
        client.test_connection()
        print("  OK -- credentials and signing work.")
        if args.id:
            print(f"\nGET {args.type}/{args.id} (read-only):")
            print(json.dumps(client.get_record(args.type, args.id), indent=2, default=str))
    except NetSuiteUnavailable as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
