# Workflow: Push Crstl invoices to NetSuite (as sales orders)

**Objective:** Get Crstl (Home Depot Canada) invoices into NetSuite as **sales
order** records automatically, so accounting can match them against orders and
then against payables instead of keying them by hand. Mirrors how OMIS already
pushes sales orders into the same NetSuite account.

## Inputs
- `.tmp/invoices_raw.json` — from `tools/crstl_fetch_invoices.py`.
- Province/store attached to each invoice (via `app.main._attach_provinces`).
  Unmapped invoices are skipped, not guessed.
- Internal ids filled in `config/netsuite_customers.json` (`item_ids`,
  `tax_code_ids`, and if the account requires them, `class_id` / `subsidiary_id`).
- For `--live` only: `NETSUITE_*` TBA credentials in `.env` (see `.env.example`).

## Tools
- `tools/netsuite_probe.py` — read-only: test the connection, and GET an existing
  (e.g. OMIS-created) sales order to read off the item/tax/class ids.
- `tools/push_invoices_to_netsuite.py` — orchestrates the push.
- `app/netsuite.py::transform_invoice` — business mapping (customer, tax, lines).
- `app/netsuite_payload.py::build_sales_order_payload` — REST body, internal-id refs.
- `app/netsuite_client.py::NetSuiteClient` — TBA-signed transport.

## Steps
1. Fetch invoices: `python tools/crstl_fetch_invoices.py`. Ensure province/store
   are attached.
2. **Dry run (no credentials needed):**
   `python tools/push_invoices_to_netsuite.py`
   Review the printed sales-order bodies and the list of any missing ids.
3. **Fill the internal ids** in `config/netsuite_customers.json`. Get them from
   NetSuite — the fastest read-only way once credentials exist is
   `python tools/netsuite_probe.py --id <an existing OMIS sales order id>` and
   copy the `item` / `taxCode` / `class` ids off a real accepted record.
4. **When TBA credentials exist** (there is no sandbox — production only), set
   them in `.env`, confirm read-only first:
   `python tools/netsuite_probe.py`
5. **Send ONE controlled record first** (no sandbox to absorb a mistake):
   `python tools/push_invoices_to_netsuite.py --live --only <transaction_id>`
   against a safe/test customer where possible. Verify it in NetSuite, then widen.
6. Full run: `python tools/push_invoices_to_netsuite.py --live`.

## Edge cases / notes
- **Record type is salesOrder, refs are internal ids.** Settled by reading OMIS's
  own integration (`gems/omis-netsuite`), which references item/tax/customer/class
  purely by internal id in this same account. `build_sales_order_payload` emits
  `{"id": ...}`; unresolved names emit an empty id and BLOCK a live send.
- **Idempotency** is by external id (Crstl `transaction_id`, the only field unique
  per invoice) — re-running updates the same sales order, never duplicates.
- **No sandbox.** Do the read-only probe and single `--only` record before any
  batch. Credentials can write; keep them only in `.env`.
- A missing send is a gap to retry; a wrong send is an error. Prefer skipping an
  unmapped invoice over sending a guess.
