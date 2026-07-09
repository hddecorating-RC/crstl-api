"""
crstl_discover.py
-----------------
Probes candidate CRSTL API endpoint paths and prints a table showing which
ones respond, how many records they return, and what field names they expose.

Run this FIRST before configuring crstl_fetch_invoices.py.

Usage:
    python tools/crstl_discover.py

After running, copy the confirmed endpoint paths into .env:
    CRSTL_ENDPOINT_INVOICES=/invoices
    CRSTL_ENDPOINT_PAYMENTS=/payments
"""

import os
import sys
import json
import pathlib
import requests


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_env(path=".env"):
    env_file = pathlib.Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


load_env()

API_KEY  = os.environ.get("CRSTL_API_KEY", "")
BASE_URL = os.environ.get("CRSTL_BASE_URL", "https://api.crstl.ai/v1").rstrip("/")

CANDIDATE_PATHS = [
    "/invoices",
    "/invoice",
    "/edi/810",
    "/edi/invoices",
    "/transactions",
    "/documents",
    "/orders",
    "/payments",
    "/payment",
    "/edi/820",
    "/edi/payments",
    "/retailers",
    "/trading-partners",
    "/v1/invoices",
    "/v2/invoices",
    "/api/invoices",
    "/api/v1/invoices",
]


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------

def probe(path: str) -> dict:
    url = BASE_URL + path
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(url, headers=headers, params={"limit": 1, "page_size": 1}, timeout=10)
        status = resp.status_code

        if status == 401:
            return {"path": path, "status": status, "note": "Unauthorized — check API key"}
        if status == 403:
            return {"path": path, "status": status, "note": "Forbidden"}
        if status == 404:
            return {"path": path, "status": status, "note": "Not found"}
        if status >= 500:
            return {"path": path, "status": status, "note": "Server error"}

        try:
            data = resp.json()
        except Exception:
            return {"path": path, "status": status, "note": "Non-JSON response"}

        # Unwrap common envelope patterns
        records = None
        top_keys = []
        if isinstance(data, list):
            records = data
        elif isinstance(data, dict):
            top_keys = list(data.keys())
            for key in ("data", "invoices", "payments", "results", "items", "records"):
                if key in data and isinstance(data[key], list):
                    records = data[key]
                    break

        sample_keys = []
        record_count = "?"
        if records is not None:
            record_count = len(records)
            if records:
                sample_keys = list(records[0].keys()) if isinstance(records[0], dict) else []

        return {
            "path": path,
            "status": status,
            "top_keys": top_keys,
            "record_count": record_count,
            "sample_keys": sample_keys,
        }

    except requests.exceptions.ConnectionError:
        return {"path": path, "status": "ERR", "note": "Connection refused"}
    except requests.exceptions.Timeout:
        return {"path": path, "status": "ERR", "note": "Timeout"}
    except Exception as e:
        return {"path": path, "status": "ERR", "note": str(e)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not API_KEY:
        print("ERROR: CRSTL_API_KEY not set. Add it to .env and retry.")
        sys.exit(1)

    print(f"\nProbing {len(CANDIDATE_PATHS)} candidate paths against: {BASE_URL}\n")
    print(f"{'PATH':<30} {'STATUS':<8} {'RECORDS':<10} {'SAMPLE KEYS / NOTE'}")
    print("-" * 90)

    hits = []
    for path in CANDIDATE_PATHS:
        result = probe(path)
        status  = str(result["status"])
        records = str(result.get("record_count", ""))
        note    = result.get("note", "")
        sample  = ", ".join(result.get("sample_keys", [])[:8])  # first 8 keys
        top     = ", ".join(result.get("top_keys", []))

        detail = sample or note or (f"top-level keys: {top}" if top else "")

        # Highlight successful responses
        marker = "✓" if result["status"] == 200 else " "
        print(f"{marker} {path:<28} {status:<8} {records:<10} {detail}")

        if result["status"] == 200:
            hits.append(result)

    print()
    if hits:
        print("=== SUCCESSFUL ENDPOINTS ===")
        for h in hits:
            print(f"\n  Path:        {h['path']}")
            print(f"  Records:     {h['record_count']}")
            if h.get("sample_keys"):
                print(f"  Field names: {', '.join(h['sample_keys'])}")
            if h.get("top_keys"):
                print(f"  Top-level:   {', '.join(h['top_keys'])}")
        print()
        print("Next steps:")
        print("  1. Add confirmed paths to .env:")
        print("       CRSTL_ENDPOINT_INVOICES=<path>")
        print("       CRSTL_ENDPOINT_PAYMENTS=<path>")
        print("  2. Update field name mappings in tools/crstl_fetch_invoices.py")
        print("  3. Run: python tools/crstl_fetch_invoices.py")
    else:
        print("No endpoints returned 200. Check your CRSTL_API_KEY and CRSTL_BASE_URL in .env.")


if __name__ == "__main__":
    main()
