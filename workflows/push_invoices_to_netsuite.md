# Workflow: Push Crstl invoices to NetSuite

**Objective:** Get Crstl (Home Depot Canada) invoices into NetSuite as invoice
records automatically, so accounting stops keying them by hand. Replaces the
manual CSV import.

## Inputs
- `.tmp/invoices_raw.json` — from `tools/crstl_fetch_invoices.py`.
- Province/store attached to each invoice (via `app.main._attach_provinces`, or
  the dashboard path). Invoices without a mapping are skipped, not guessed.
- For `--live` only: `NETSUITE_*` TBA credentials in `.env` (see `.env.example`).

## Tools
- `tools/push_invoices_to_netsuite.py` — orchestrates the push.
- `app/netsuite.py::transform_invoice` — business mapping (customer, tax, lines).
- `app/netsuite_payload.py::build_invoice_payload` — NetSuite REST body.
- `app/netsuite_client.py::NetSuiteClient` — TBA-signed transport.

## Steps
1. Fetch invoices: `python tools/crstl_fetch_invoices.py`.
2. Ensure province/store are attached to the invoices.
3. **Dry run (no credentials needed):**
   `python tools/push_invoices_to_netsuite.py`
   Review the printed NetSuite bodies. Confirm customer ids, tax codes, line
   amounts, and that `externalId` is the Crstl `transaction_id`.
4. **When TBA credentials exist**, set them in `.env` for the **sandbox** first,
   then: `python tools/push_invoices_to_netsuite.py --live`.
   The tool calls `test_connection()` before sending.
5. Verify the records in NetSuite. Because the upsert is keyed by `externalId`,
   re-running is idempotent — the same invoice updates in place, never duplicates.
6. Promote to the production NetSuite account only after sandbox is clean.

## Edge cases / notes
- **The one unverified seam is the REST body shape** (`build_invoice_payload`):
  whether `item`/`taxCode`/`currency` want `refName` (names) or `id` (internal
  ids). transform_invoice carries names, so `refName` is the starting bet; if the
  sandbox rejects a name, switch that ref to an internal id. This can only be
  settled against a live sandbox.
- **Idempotency** is by external id (Crstl `transaction_id`) — the only field
  observed unique per invoice. PO and invoice numbers collide.
- **Credentials can write.** Sandbox first; never commit real values.
- A missing send is a gap to retry; a wrong send is an error. Prefer skipping an
  unmapped invoice over sending a guess.
