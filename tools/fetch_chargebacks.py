"""Fetch all 812 credit/debit adjustments (chargebacks) from Crstl and cache them.

Home Depot Canada sends chargebacks as incoming 812 documents. Each carries a
header-level total (`cd_amount`) and a detail loop of individual adjustments,
where `cd_adjustment.id` encodes the X12 adjustment reason code as a
`<name>_<code>` suffix (e.g. `restocking_charge_B7` -> code B7).

Writes the raw details to .tmp/chargebacks_raw.json so downstream analysis can
run repeatedly without re-hitting the paid API.

Usage:
    python3 tools/fetch_chargebacks.py [--refresh]

Without --refresh, exits early if the cache already exists.
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.crstl import CrstlClient
from app.main import load_env

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(REPO, ".tmp", "chargebacks_raw.json")


def fetch_all(client: CrstlClient) -> list[dict]:
    txs = client._fetch_all_transactions(transaction_type="812")
    tx_ids = [t.get("id") or t.get("document_id") for t in txs]
    tx_ids = [t for t in tx_ids if t]
    print(f"Found {len(tx_ids)} 812 transactions; fetching detail...")

    details = []
    with ThreadPoolExecutor(max_workers=client.MAX_WORKERS) as pool:
        futures = {pool.submit(client._fetch_transaction_detail, tid): tid for tid in tx_ids}
        for i, future in enumerate(as_completed(futures), 1):
            tid = futures[future]
            try:
                details.append(future.result())
            except Exception as exc:
                print(f"  WARNING: failed to fetch detail for {tid}: {exc}")
            if i % 25 == 0:
                print(f"  {i}/{len(tx_ids)}")
    return details


def main() -> None:
    refresh = "--refresh" in sys.argv
    if os.path.exists(CACHE_PATH) and not refresh:
        with open(CACHE_PATH) as fh:
            cached = json.load(fh)
        print(f"Cache exists: {CACHE_PATH} ({len(cached)} records). Use --refresh to re-fetch.")
        return

    load_env(os.path.join(REPO, ".env"))
    client = CrstlClient(os.environ["CRSTL_BASE_URL"], os.environ["CRSTL_API_KEY"])

    details = fetch_all(client)
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as fh:
        json.dump(details, fh, indent=2)
    print(f"Wrote {len(details)} records to {CACHE_PATH}")


if __name__ == "__main__":
    main()
