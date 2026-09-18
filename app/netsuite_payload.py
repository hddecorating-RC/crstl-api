"""
Assemble a NetSuite REST `invoice` body from the flat line records that
app.netsuite.transform_invoice emits (all of which share one external_id).

Record type: accounting books this Home Depot Canada revenue as an INVOICE.

WHY internal ids, not names: OMIS's own NetSuite integration (same account)
references item, taxCode, customer and class purely by internal id
(BaseReference internal_id). This account is set up for ids, so the REST refs are
{"id": ...}. transform_invoice carries human-readable item/tax NAMES (built for
the CSV path), so this module resolves those names to internal ids via
config/netsuite_customers.json:
  - item_ids:      {item name -> internal id}
  - tax_code_ids:  {tax code name -> internal id}
  - class_id / subsidiary_id: optional account-level refs

Fill those ids in the config (look them up in NetSuite -- tools/netsuite_probe.py
GETs an existing record, and Lists/Setup pages show ids with "Show Internal IDs"
on). Until an id is filled the ref is emitted empty; unresolved_ids() reports
which, and the push tool refuses a live send while any remain.
"""
import functools
import json
import pathlib

_CONFIG = pathlib.Path(__file__).parent.parent / "config" / "netsuite_customers.json"


@functools.lru_cache(maxsize=None)
def load_refs() -> dict:
    """The internal-id maps from config, defaulted so a missing block is empty
    rather than a KeyError."""
    cfg = json.loads(_CONFIG.read_text())
    return {
        "item_ids": cfg.get("item_ids", {}),
        "tax_code_ids": cfg.get("tax_code_ids", {}),
        "class_id": cfg.get("class_id", ""),
        "subsidiary_id": cfg.get("subsidiary_id", ""),
        "custom_form_id": cfg.get("custom_form_id", ""),
        # Which record the connector creates ("invoice" or "salesOrder"), and the
        # SO-only refs (order status + its own custom form). Default "invoice" so an
        # old config with no push block behaves exactly as before.
        "record_type": cfg.get("record_type", "invoice"),
        "sales_order": cfg.get("sales_order", {}),
        # Scheduled-job-only guards (go-live cutoff + per-run cap). Read by the
        # push job; the shared push path ignores it so manual pushes are unlimited.
        "automation": cfg.get("automation", {}),
        # Finale invoicing (app.finale_invoice): promo-preset + tax-rate ids per
        # channel/province, its own enabled flag and per-run cap. Default {} so a
        # config without the block leaves Finale invoicing off and unresolved.
        "finale": cfg.get("finale", {}),
        # ShipStation DSD close (app.shipstation): store id, floor, cap, enabled flag.
        "shipstation": cfg.get("shipstation", {}),
        # Order alerts (app.alerts): issue thresholds, store id, enabled flag.
        "alerts": cfg.get("alerts", {}),
        # Dropship pre-fill (app.dropship): store id, floor, cap, enabled flag.
        "dropship_prefill": cfg.get("dropship_prefill", {}),
    }


def unresolved_ids(line_records: list[dict], refs: dict | None = None) -> list[str]:
    """Item/tax names on these lines that still lack an internal id in config.
    Returns tags like 'item:Merchandise Sales', 'taxCode:CA-HST ONT' (deduped,
    order-preserving). A live send is blocked while this is non-empty."""
    refs = refs or load_refs()
    missing: list[str] = []
    for r in line_records:
        for tag in (f'item:{r["item"]}' if not refs["item_ids"].get(r["item"]) else None,
                    f'taxCode:{r["tax_code"]}' if not refs["tax_code_ids"].get(r["tax_code"]) else None):
            if tag and tag not in missing:
                missing.append(tag)
    return missing


def _item_sublist(line_records: list[dict], refs: dict) -> dict:
    """The `item` sublist shared by the invoice and sales-order bodies. Item/tax
    names resolve to internal ids; an unresolved name emits an empty id (see
    unresolved_ids), which blocks a live send."""
    return {
        "items": [
            {
                "item": {"id": refs["item_ids"].get(r["item"], "")},
                "description": r.get("description", ""),
                "quantity": r["quantity"],
                "rate": r["rate"],
                "amount": r["amount"],
                "taxCode": {"id": refs["tax_code_ids"].get(r["tax_code"], "")},
            }
            for r in line_records
        ]
    }


def build_payload(line_records: list[dict], refs: dict | None = None,
                  record_type: str | None = None) -> dict:
    """Dispatch to the right REST body for the configured record type. Both bodies
    share the customer, item sublist, tax, memo and Lead #; they differ only in the
    header fields NetSuite requires for that record (see each builder). record_type
    defaults to config (refs["record_type"]), so callers need not thread it."""
    refs = refs or load_refs()
    record_type = record_type or refs.get("record_type") or "invoice"
    if record_type == "salesOrder":
        return build_sales_order_payload(line_records, refs)
    return build_invoice_payload(line_records, refs)


def build_invoice_payload(line_records: list[dict], refs: dict | None = None) -> dict:
    """One NetSuite REST invoice body from transform_invoice's line records.

    Refs are internal ids ({"id": ...}); an unresolved item/tax name emits an
    empty id (see unresolved_ids). Amounts and the customer id come straight from
    the mapped records -- only the item/tax/class refs are resolved here. Raises
    ValueError on an empty list.
    """
    if not line_records:
        raise ValueError("build_invoice_payload needs at least one line record")
    refs = refs or load_refs()
    head = line_records[0]

    payload = {
        "externalId": head["external_id"],
        "entity": {"id": head["customer_id"]},
        "tranDate": head["tran_date"],
        "memo": head["memo"],
        # NetSuite's "LEAD #" field is otherRefNum on form 101. Accounting puts
        # the INV-prefixed Crstl invoice number there (e.g. INV40861211), so use
        # invoice_number, falling back to the PO/order number if absent.
        "otherRefNum": head.get("invoice_number") or head["other_ref_num"],
        # "INVOICE %" (custbodyinvoicepercent) defaults to 50 on the form -- that
        # is OMIS's deposit model (50% now, 50% second payment). Home Depot
        # invoices are billed in full, so set it to 100.
        "custbodyinvoicepercent": 100,
        "item": _item_sublist(line_records, refs),
    }
    # Optional fields -- only sent when present, so NetSuite derives/rejects nothing.
    if head.get("due_date"):
        payload["dueDate"] = head["due_date"]  # NetSuite rejects an empty date string
    if refs.get("class_id"):
        payload["class"] = {"id": refs["class_id"]}
    if refs.get("subsidiary_id"):
        payload["subsidiary"] = {"id": refs["subsidiary_id"]}
    if refs.get("custom_form_id"):
        payload["customForm"] = {"id": refs["custom_form_id"]}  # 101 "Custom Service Invoice"
    return payload


def build_sales_order_payload(line_records: list[dict], refs: dict | None = None) -> dict:
    """One NetSuite REST salesOrder body from transform_invoice's line records.

    Accounting pivoted to Sales Orders (2026-09-11): the connector creates the SO,
    their team converts it to an invoice. Same customer/item/tax/discount/Lead # as
    the invoice body -- it differs only in the header:
      * orderStatus (from config sales_order.order_status_id; "A" = Pending
        Approval, which parks the SO in an approval queue -- a human gate before
        anything can bill to the GL). NB: NetSuite only honors "A" when SO approval
        routing is enabled; otherwise it silently creates the SO as "B".
      * customForm is the SO's own form (config sales_order.custom_form_id), NOT the
        invoice form 101.
      * custbodyinvoicepercent = 100: form 102 (OMIS's form) carries the "INVOICE %"
        field and DEFAULTS it to 50 (OMIS's 50%-now/50%-later deposit model); HD
        Canada bills in full, so force 100 -- same as the invoice path.
      * no dueDate (an SO carries terms, not a due date).
    """
    if not line_records:
        raise ValueError("build_sales_order_payload needs at least one line record")
    refs = refs or load_refs()
    head = line_records[0]
    so = refs.get("sales_order") or {}

    payload = {
        "externalId": head["external_id"],
        "entity": {"id": head["customer_id"]},
        "tranDate": head["tran_date"],
        "memo": head["memo"],
        "otherRefNum": head.get("invoice_number") or head["other_ref_num"],
        # Form 102 defaults "INVOICE %" to 50 (OMIS's 50/50 deposit split); HD Canada
        # is billed in full, so force 100 (same as the invoice body).
        "custbodyinvoicepercent": 100,
        "item": _item_sublist(line_records, refs),
    }
    # Optional fields -- only sent when present, so NetSuite derives/rejects nothing.
    if so.get("order_status_id"):
        payload["orderStatus"] = {"id": so["order_status_id"]}  # "A" = Pending Approval
    if refs.get("class_id"):
        payload["class"] = {"id": refs["class_id"]}
    if refs.get("subsidiary_id"):
        payload["subsidiary"] = {"id": refs["subsidiary_id"]}
    if so.get("custom_form_id"):
        payload["customForm"] = {"id": so["custom_form_id"]}
    return payload
