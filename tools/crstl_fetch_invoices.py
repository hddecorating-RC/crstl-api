"""
crstl_fetch_invoices.py
-----------------------
Fetches EDI 810 invoice transactions from the Crstl v2 API, pulls full details
per invoice (for subtotals and taxes), and writes .tmp/invoices_raw.json.

Usage:
    python tools/crstl_fetch_invoices.py

Prerequisites:
    - Set CRSTL_EMAIL, CRSTL_PASSWORD, CRSTL_ORG_ID, CRSTL_BASE_URL in .env

Output:
    .tmp/invoices_raw.json  — list of normalized invoice dicts
"""

import os
import sys
import json
import pathlib
from datetime import datetime, timezone

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

EMAIL      = os.environ.get("CRSTL_EMAIL", "")
PASSWORD   = os.environ.get("CRSTL_PASSWORD", "")
ORG_ID     = os.environ.get("CRSTL_ORG_ID", "")
BASE_URL   = os.environ.get("CRSTL_BASE_URL", "https://api.crstl.ai/v2").rstrip("/")
PAGE_SIZE  = int(os.environ.get("CRSTL_PAGE_SIZE", "100"))

TOKEN_CACHE = pathlib.Path(".tmp/crstl_tokens.json")
OUTPUT_PATH = pathlib.Path(".tmp/invoices_raw.json")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def load_cached_tokens():
    if TOKEN_CACHE.exists():
        try:
            return json.loads(TOKEN_CACHE.read_text())
        except Exception:
            pass
    return {}


def save_tokens(tokens):
    TOKEN_CACHE.parent.mkdir(exist_ok=True)
    TOKEN_CACHE.write_text(json.dumps(tokens, indent=2))


def token_is_valid(tokens):
    expires_at = tokens.get("access_token_expires_at")
    if not expires_at or not tokens.get("access_token"):
        return False
    try:
        exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        # treat as expired 60s early to avoid edge cases
        return exp > datetime.now(timezone.utc).replace(second=0, microsecond=0)
    except Exception:
        return False


def get_access_token():
    tokens = load_cached_tokens()

    if token_is_valid(tokens):
        return tokens["access_token"]

    # Try refresh first
    if tokens.get("refresh_token"):
        print("  Refreshing access token...")
        resp = requests.post(
            f"{BASE_URL}/auth/refresh",
            json={"refresh_token": tokens["refresh_token"]},
            timeout=30,
        )
        if resp.status_code == 200:
            new_tokens = resp.json()
            save_tokens(new_tokens)
            return new_tokens["access_token"]
        else:
            print(f"  Refresh failed ({resp.status_code}), re-authenticating...")

    # Full login
    print("  Authenticating with email/password...")
    payload = {"email": EMAIL, "password": PASSWORD}
    if ORG_ID:
        payload["organization_id"] = ORG_ID

    resp = requests.post(f"{BASE_URL}/auth/token", json=payload, timeout=30)
    if resp.status_code != 200:
        print(f"  ERROR: Auth failed ({resp.status_code}): {resp.text[:300]}")
        sys.exit(1)

    tokens = resp.json()
    save_tokens(tokens)
    print(f"  Authenticated. Token expires: {tokens.get('access_token_expires_at')}")
    return tokens["access_token"]


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

def api_get(path, params=None, token=None):
    resp = requests.get(
        BASE_URL + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_all_transactions(token, transaction_type="810"):
    """Paginate through all transactions of a given EDI type."""
    results = []
    params = {
        "transaction_type": transaction_type,
        "limit": PAGE_SIZE,
    }
    cursor = None

    while True:
        if cursor:
            params["cursor"] = cursor

        data = api_get("/transaction", params=params, token=token)

        # Unwrap envelope: {status, code, data: {count, transactions: [...]}}
        inner = data.get("data", data)
        records = inner.get("transactions") or inner.get("results") or inner.get("items") or []

        if not isinstance(records, list):
            print(f"  WARNING: Unexpected transactions shape. Keys: {list(inner.keys())}")
            break

        results.extend(records)
        cursor = inner.get("next_cursor") or inner.get("cursor")

        if not cursor or len(records) < PAGE_SIZE:
            break

    return results


def fetch_transaction_detail(transaction_id, token):
    """Fetch full EDI document detail for a single transaction."""
    data = api_get(f"/transaction/{transaction_id}", token=token)
    return data.get("data", data)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def parse_float(val):
    if val is None:
        return 0.0
    try:
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def extract_invoice_fields(detail):
    """
    Pull accounting fields from a full transaction detail record.
    transaction_data holds the EDI 810 payload; field names may vary by
    trading partner config — tries common candidates in order.
    """
    tx_data = detail.get("transaction_data") or detail.get("document_data") or {}

    def first(*keys):
        for k in keys:
            v = tx_data.get(k) or detail.get(k)
            if v is not None and v != "":
                return v
        return None

    total_amount = parse_float(first("total_amount", "invoice_amount", "amount"))

    # Subtotal: sum of non-tax line items if invoice_lines present, else total
    invoice_lines = tx_data.get("invoice_lines") or tx_data.get("line_items") or []
    subtotal = 0.0
    for line in invoice_lines:
        subtotal += parse_float(line.get("line_amount") or line.get("amount") or line.get("extended_amount"))

    # Tax: look for explicit tax fields; fall back to total - subtotal
    tax_amount = parse_float(
        first("tax_amount", "tax_total", "total_tax", "txi_amount")
        or tx_data.get("taxes", {}).get("total") if isinstance(tx_data.get("taxes"), dict) else None
    )
    if tax_amount == 0.0 and subtotal > 0.0 and total_amount > subtotal:
        tax_amount = round(total_amount - subtotal, 2)

    if subtotal == 0.0:
        subtotal = round(total_amount - tax_amount, 2)

    return {
        "invoice_number":   str(first("invoice_number", "document_number", "number") or ""),
        "po_number":        str(detail.get("reference_id") or first("purchase_order_number", "po_number") or ""),
        "trading_partner":  str(detail.get("trading_partner_name") or first("trading_partner", "retailer") or ""),
        "invoice_date":     str(first("invoice_date", "issue_date", "date") or ""),
        "due_date":         str(first("due_date", "payment_due_date") or ""),
        "status":           str(detail.get("state") or detail.get("status") or first("status") or ""),
        "subtotal":         round(subtotal, 2),
        "tax_amount":       round(tax_amount, 2),
        "total_amount":     round(total_amount, 2),
        "currency":         str(first("currency", "currency_code") or "USD"),
        "transaction_id":   str(detail.get("id") or detail.get("transaction_id") or ""),
        "created_at":       str(detail.get("created_at") or ""),
        "_raw":             detail,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    missing = [k for k, v in [("CRSTL_EMAIL", EMAIL), ("CRSTL_PASSWORD", PASSWORD)] if not v]
    if missing:
        print(f"ERROR: Missing required env vars: {', '.join(missing)}")
        print("Add them to .env and retry.")
        sys.exit(1)

    print(f"Base URL: {BASE_URL}")
    print()

    print("Step 1: Authenticating...")
    token = get_access_token()

    print("\nStep 2: Fetching EDI 810 invoice transactions...")
    transactions = fetch_all_transactions(token, transaction_type="810")
    print(f"  Found {len(transactions)} invoice transactions")

    if not transactions:
        print("No invoice transactions found. Check your credentials and org ID.")
        sys.exit(0)

    print("\nStep 3: Fetching full details per invoice...")
    invoices = []
    for i, tx in enumerate(transactions, 1):
        tx_id = tx.get("id") or tx.get("transaction_id")
        if not tx_id:
            print(f"  [{i}/{len(transactions)}] SKIP — no id in record")
            continue

        print(f"  [{i}/{len(transactions)}] {tx_id}  ref={tx.get('reference_id', '')}", end="  ")
        try:
            detail = fetch_transaction_detail(tx_id, token)
            invoice = extract_invoice_fields(detail)
            invoices.append(invoice)
            print(f"total={invoice['total_amount']}  status={invoice['status']}")
        except requests.exceptions.HTTPError as e:
            print(f"ERROR {e.response.status_code}: {e.response.text[:100]}")

    print(f"\n  Extracted {len(invoices)} invoices")

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(invoices, indent=2, default=str))
    print(f"Wrote {len(invoices)} records to {OUTPUT_PATH}")
    print("Next: python tools/generate_accounting_csv.py")


if __name__ == "__main__":
    main()
