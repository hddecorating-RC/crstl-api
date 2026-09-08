# Workflow: Push Crstl invoices to NetSuite (as invoices)

**Objective:** Get Crstl (Home Depot Canada) invoices into NetSuite as **invoice**
records automatically, so accounting stops keying them by hand. Refs are internal
ids (the convention OMIS uses in the same account).

## Inputs
- `.tmp/invoices_raw.json` — from `tools/crstl_fetch_invoices.py`.
- Province/store attached to each invoice (via `app.main._attach_provinces`).
  Unmapped invoices are skipped, not guessed.
- Internal ids filled in `config/netsuite_customers.json` (`item_ids`,
  `tax_code_ids`, and if the account requires them, `class_id` / `subsidiary_id`).
- `NETSUITE_*` TBA credentials in `.env` (see `.env.example`).

## Role permissions (NetSuite)
The integration role needs, at minimum: **Log in using Access Tokens** + **REST
Web Services** (Setup), **Invoice** create (Transactions), and View on **Items /
Customers / Tax Records / Classifications** (Lists). A collection GET (list) is a
*search* and some roles refuse it — the connector avoids listing, so that is fine.

## Tools
- `tools/netsuite_probe.py` — read-only: test the connection (invoice metadata),
  and GET an existing record by internal id to read item/tax/class ids.
- `tools/push_invoices_to_netsuite.py` — orchestrates the push.
- `app/netsuite.py::transform_invoice` — business mapping (customer, tax, lines).
- `app/netsuite_payload.py::build_invoice_payload` — REST body, internal-id refs.
- `app/netsuite_client.py::NetSuiteClient` — TBA-signed transport.

## Steps
1. Fetch invoices: `python tools/crstl_fetch_invoices.py`. Ensure province/store
   are attached.
2. **Dry run (no credentials needed):** `python tools/push_invoices_to_netsuite.py`
   Review the printed invoice bodies and the list of any missing ids.
3. **Fill the internal ids** in `config/netsuite_customers.json`. Turn on
   "Show Internal IDs" (Home → Set Preferences → General) and read them from
   Lists → Items, Setup → Accounting → Tax Codes, etc. — or
   `python tools/netsuite_probe.py --id <internal id of an existing invoice>`.
4. **Confirm connection (read-only):** `python tools/netsuite_probe.py`
5. **Send ONE controlled record first** (no sandbox):
   `python tools/push_invoices_to_netsuite.py --live --only <transaction_id>`
   Verify it in NetSuite, then widen.
6. Full run: `python tools/push_invoices_to_netsuite.py --live`.

## Edge cases / notes
- **Record type is invoice; refs are internal ids.** `build_invoice_payload`
  emits `{"id": ...}`; unresolved names emit an empty id and BLOCK a live send.
- **Idempotency** is by external id (Crstl `transaction_id`, unique per invoice) —
  re-running updates the same invoice, never duplicates.
- **Invoices post to the GL.** With no sandbox, do the read-only probe and one
  `--only` record before any batch. Credentials can write; keep them only in `.env`.
- A missing send is a gap to retry; a wrong send is an error. Prefer skipping an
  unmapped invoice over sending a guess.
