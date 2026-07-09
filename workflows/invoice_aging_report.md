# Workflow: Daily Invoice Export for Accounting

## Objective
Fetch EDI 810 invoice transactions from Crstl daily and produce a CSV with invoice
status, subtotals, and taxes for accounting to import into NetSuite.

## Required Inputs (set in `.env`)
- `CRSTL_EMAIL` — your Crstl login email
- `CRSTL_PASSWORD` — your Crstl login password
- `CRSTL_ORG_ID` — your Crstl organization ID (from support@crstl.so if unknown)
- `CRSTL_BASE_URL` — `https://api.crstl.ai/v2` (do not change)

## Steps

### Step 1: Fetch invoices

```bash
python tools/crstl_fetch_invoices.py
```

Authenticates with email/password, caches the token in `.tmp/crstl_tokens.json`,
fetches all EDI 810 transactions, pulls full detail per invoice (for subtotals and taxes),
and writes `.tmp/invoices_raw.json`.

Expected output:
```
Base URL: https://api.crstl.ai/v2

Step 1: Authenticating...
  Authenticated. Token expires: 2026-07-09T12:00:00Z

Step 2: Fetching EDI 810 invoice transactions...
  Found 47 invoice transactions

Step 3: Fetching full details per invoice...
  [1/47] abc123  ref=INV-2025-001  total=4500.00  status=Open
  ...

Wrote 47 records to .tmp/invoices_raw.json
```

### Step 2: Generate accounting CSV

```bash
python tools/generate_accounting_csv.py
```

Reads `.tmp/invoices_raw.json` and writes `.tmp/invoices_YYYY-MM-DD.csv`.

Expected output:
```
Wrote 47 invoices to .tmp/invoices_2026-07-09.csv

Summary:
  Invoices:  47
  Subtotal:  $84,200.00
  Tax:       $5,100.00
  Total:     $89,300.00
  Statuses:  {'Open': 38, 'Completed': 9}

Send .tmp/invoices_2026-07-09.csv to accounting for NetSuite import.
```

### Step 3: Send CSV to accounting

Email `.tmp/invoices_YYYY-MM-DD.csv` to accounting.

**CSV columns:**
| Column | Description |
|---|---|
| Invoice Number | EDI 810 invoice number |
| PO Number | Home Depot purchase order number |
| Trading Partner | Retailer name |
| Invoice Date | Date invoice was issued |
| Due Date | Payment due date |
| Status | Transaction state (Open, Completed, etc.) |
| Subtotal | Line item total before tax |
| Tax Amount | Tax charged |
| Total Amount | Grand total (subtotal + tax) |
| Currency | USD |
| Transaction ID | Crstl internal ID |
| Created At | When the transaction was created in Crstl |

## Running Daily (macOS cron)

Add to crontab (`crontab -e`) to run every weekday at 7am:

```
0 7 * * 1-5 cd /Users/ritchiecheung/Work/crstl-api && python tools/crstl_fetch_invoices.py && python tools/generate_accounting_csv.py
```

## Token Caching

Tokens are cached in `.tmp/crstl_tokens.json` and reused until expiry.
The script auto-refreshes using the refresh token so re-entering credentials is rare.
Do not commit `.tmp/` to git.

## Edge Cases

| Situation | Behavior |
|---|---|
| Token expired | Script uses refresh token automatically; falls back to full re-login |
| Invoice has no line items | Subtotal defaults to `total - tax`; tax defaults to 0 |
| No invoices found | Script exits with a message — check credentials and org ID |
| Detail fetch fails for one invoice | Logged and skipped; rest continue |

## Troubleshooting

**Auth fails (401)**
Check `CRSTL_EMAIL`, `CRSTL_PASSWORD`, `CRSTL_ORG_ID` in `.env`. Delete
`.tmp/crstl_tokens.json` to force a fresh login.

**0 transactions returned**
Your org may use a different transaction type code. Check with support@crstl.so
whether 810 invoices are enabled for your trading partnership.

**Subtotal and tax both show 0**
The trading partner's EDI mapping may not include line-item breakdowns.
`total_amount` will still be correct. Ask Crstl support if line-item detail is available.

## Future: Option 2 — Direct NetSuite Integration

When ready, replace Step 3 with `tools/push_to_netsuite.py` that POSTs records
directly to NetSuite via REST API. Requires:
- NetSuite integration record + OAuth 2.0 setup (NetSuite admin access needed)
- NetSuite account ID and client credentials in `.env`

## Improvements discovered
- (Add actual status values once confirmed from first run)
- (Add correct org ID once retrieved from Crstl support)
