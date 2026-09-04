"""Audit DSD ASN (856) REF*CN values for RTS numbers sent in place of HD Shipment IDs.

Home Depot Canada matches an inbound ASN to its planned shipment using the
Home Depot Shipment ID, which their transportation planning system generates
and which always begins with 61. That value belongs in the REF*CN segment
("Carrier's Reference Number (PRO/Invoice)"), surfaced by Crstl as
`carrier_reference_number` on each shipment in the 856.

RTS numbers begin with 32 and belong in the BOL field, not REF*CN. When an RTS
number lands in REF*CN, HD's ASN validation cannot match the shipment.

Classification of `carrier_reference_number`:
    61...   -> ok        (HD Shipment ID, correct)
    32...   -> rts_error (RTS number in REF*CN, incorrect)
    empty   -> missing   (no REF*CN sent at all)
    other   -> unknown   (neither prefix — needs eyeballing)

Only ASNs whose trading_partner_flavor is "Direct Store Delivery (DSD)" are
considered; Dropship and Wholesale ASNs use a different routing model.

Usage:
    python3 tools/audit_dsd_asn_refcn.py [--months N] [--refresh]

Outputs:
    .tmp/dsd_asn_refcn.json  — one record per DSD ASN in the window
    .tmp/dsd_asn_refcn.csv   — same, flat, for sharing
    Console summary + the list of offending ASNs.
"""
import argparse
import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.crstl import CrstlClient
from app.main import load_env

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_CACHE = os.path.join(REPO, ".tmp", "asn_856_list.json")
DETAIL_CACHE = os.path.join(REPO, ".tmp", "dsd_asn_details.json")
OUT_JSON = os.path.join(REPO, ".tmp", "dsd_asn_refcn.json")
OUT_CSV = os.path.join(REPO, ".tmp", "dsd_asn_refcn.csv")

DSD_FLAVOR = "Direct Store Delivery (DSD)"


def fetch_list(client: CrstlClient) -> list[dict]:
    """Page through every outgoing 856. The server caps pages at 20 rows."""
    results, offset = [], 0
    while True:
        resp = client.session.get(
            f"{client.base_url}/transaction",
            params={"transaction_type": "856", "limit": client.PAGE_SIZE, "offset": offset},
            timeout=30,
        )
        resp.raise_for_status()
        records = resp.json().get("data", {}).get("transactions") or []
        results.extend(records)
        if len(records) < client.PAGE_SIZE:
            break
        offset += client.PAGE_SIZE
    return results


def classify(ref: str) -> str:
    ref = (ref or "").strip()
    if not ref:
        return "missing"
    if ref.startswith("61"):
        return "ok"
    if ref.startswith("32"):
        return "rts_error"
    return "unknown"


def extract(tx: dict, detail: dict) -> list[dict]:
    """One row per shipment in the ASN — an 856 can carry more than one."""
    edi = detail.get("file", {}).get("generic_json_edi", {})
    heading = edi.get("heading", {})
    shipments = edi.get("detail", {}).get("shipments") or [{}]
    orders = edi.get("detail", {}).get("orders") or []
    po_numbers = sorted({str(o.get("purchase_order_number") or "") for o in orders} - {""})

    rows = []
    for idx, ship in enumerate(shipments):
        carrier = ship.get("carrier_details") or {}
        ref_cn = str(ship.get("carrier_reference_number") or "").strip()
        rows.append({
            "asn_number": str(tx.get("reference_id") or heading.get("ship_notice_number") or ""),
            "transaction_id": str(tx.get("id") or ""),
            "created_at": str(tx.get("created_at") or ""),
            "ship_notice_date": str(heading.get("ship_notice_date") or ""),
            "shipment_date": str(ship.get("shipment_date") or ship.get("shipped_date") or ""),
            "state": str((tx.get("state") or {}).get("value") or ""),
            "shipment_index": idx,
            "ref_cn": ref_cn,
            "verdict": classify(ref_cn),
            "bill_of_lading_number": str(ship.get("bill_of_lading_number") or "").strip(),
            "routing_description": str(carrier.get("routing_description") or "").strip(),
            "scac": str(carrier.get("identification_code") or "").strip(),
            "ship_to_name": str((ship.get("ship_to") or {}).get("name") or ""),
            "store": str((ship.get("ordered_by") or {}).get("location") or ""),
            "po_numbers": ",".join(po_numbers),
        })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=2, help="lookback window in months (default 2)")
    ap.add_argument("--refresh", action="store_true", help="re-fetch instead of using .tmp caches")
    args = ap.parse_args()

    load_env(os.path.join(REPO, ".env"))
    client = CrstlClient(os.environ["CRSTL_BASE_URL"], os.environ["CRSTL_API_KEY"])

    if os.path.exists(LIST_CACHE) and not args.refresh:
        transactions = json.load(open(LIST_CACHE))
        print(f"Using cached ASN list ({len(transactions)} records). --refresh to re-fetch.")
    else:
        print("Fetching all 856 transactions...")
        transactions = fetch_list(client)
        os.makedirs(os.path.dirname(LIST_CACHE), exist_ok=True)
        json.dump(transactions, open(LIST_CACHE, "w"), indent=2)
        print(f"  {len(transactions)} total 856 transactions")

    cutoff = datetime.now(timezone.utc) - timedelta(days=30 * args.months)
    cutoff_s = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    dsd = [
        t for t in transactions
        if t.get("trading_partner_flavor") == DSD_FLAVOR
        and str(t.get("created_at") or "") >= cutoff_s
    ]
    dsd.sort(key=lambda t: t.get("created_at", ""))
    print(f"\nDSD ASNs created since {cutoff_s}: {len(dsd)}")

    # Detail cache is keyed by transaction id so widening --months only costs
    # the ASNs that aren't cached yet, not a full re-fetch.
    details = {}
    if os.path.exists(DETAIL_CACHE) and not args.refresh:
        details = json.load(open(DETAIL_CACHE))
    missing = [t["id"] for t in dsd if t["id"] not in details]
    print(f"Cached details: {len(dsd) - len(missing)}/{len(dsd)}; fetching {len(missing)}...")
    if missing:
        with ThreadPoolExecutor(max_workers=client.MAX_WORKERS) as pool:
            futures = {pool.submit(client._fetch_transaction_detail, tid): tid for tid in missing}
            for i, future in enumerate(as_completed(futures), 1):
                tid = futures[future]
                try:
                    details[tid] = future.result()
                except Exception as exc:
                    print(f"  WARNING: detail fetch failed for {tid}: {exc}")
                if i % 10 == 0:
                    print(f"  {i}/{len(missing)}")
        json.dump(details, open(DETAIL_CACHE, "w"), indent=2)

    rows = []
    for tx in dsd:
        detail = details.get(tx["id"])
        if detail is None:
            continue
        rows.extend(extract(tx, detail))

    json.dump(rows, open(OUT_JSON, "w"), indent=2)
    if rows:
        with open(OUT_CSV, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    print(f"\n=== REF*CN AUDIT — {len(rows)} shipment rows across {len(dsd)} DSD ASNs ===")
    for verdict in ("ok", "rts_error", "missing", "unknown"):
        if verdict in counts:
            print(f"  {verdict:<10} {counts[verdict]}")

    bad = [r for r in rows if r["verdict"] != "ok"]
    if bad:
        print(f"\n=== {len(bad)} ASN(s) NEEDING CORRECTION ===")
        hdr = f"{'ASN':<14}{'CREATED':<12}{'STATE':<12}{'REF*CN':<14}{'BOL':<14}{'VERDICT':<11}{'PO'}"
        print(hdr)
        print("-" * len(hdr))
        for r in bad:
            print(f"{r['asn_number']:<14}{r['created_at'][:10]:<12}{r['state']:<12}"
                  f"{(r['ref_cn'] or '(empty)'):<14}{(r['bill_of_lading_number'] or '-'):<14}"
                  f"{r['verdict']:<11}{r['po_numbers']}")

    print(f"\nWrote {OUT_JSON}")
    print(f"Wrote {OUT_CSV}")


if __name__ == "__main__":
    main()
