"""
Fetch one 810 transaction from Crstl and print the raw JSON.
Run: python3 tools/inspect_810_detail.py

Used to discover what fields the API returns (ship-to province, addresses, etc.)
so we can determine if the 810 alone is sufficient for NetSuite customer routing.
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from app.main import load_env
from app.crstl import CrstlClient

load_env()

client = CrstlClient(
    base_url=os.environ.get("CRSTL_BASE_URL", "https://api.crstl.ai/v2"),
    email=os.environ.get("CRSTL_EMAIL", ""),
    password=os.environ.get("CRSTL_PASSWORD", ""),
    org_id=os.environ.get("CRSTL_ORG_ID", ""),
)

token = client.get_access_token()
transactions = client._fetch_all_transactions(token)

if not transactions:
    print("No 810 transactions found.")
    sys.exit(1)

print(f"Found {len(transactions)} transactions. Fetching detail for first one...\n")
tx_id = transactions[0].get("id") or transactions[0].get("transaction_id")
detail = client._fetch_transaction_detail(tx_id, token)

print("=== RAW DETAIL (top-level keys) ===")
for k, v in detail.items():
    if isinstance(v, dict):
        print(f"  {k}: {{...{len(v)} keys: {list(v.keys())}}}")
    elif isinstance(v, list):
        print(f"  {k}: [{len(v)} items]")
    else:
        print(f"  {k}: {v!r}")

tx_data = detail.get("transaction_data") or detail.get("document_data") or {}
print("\n=== TRANSACTION_DATA keys ===")
for k, v in tx_data.items():
    if isinstance(v, (dict, list)):
        print(f"  {k}: {type(v).__name__} — {json.dumps(v)[:120]}")
    else:
        print(f"  {k}: {v!r}")

print("\n=== FULL JSON ===")
print(json.dumps(detail, indent=2, default=str))
