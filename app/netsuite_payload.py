"""
Assemble a NetSuite REST `salesOrder` body from the flat line records that
app.netsuite.transform_invoice emits (all of which share one external_id).

WHY salesOrder, not invoice: OMIS's own NetSuite integration -- the reference
implementation in the SAME account -- creates sales orders and matches them
against orders, then against payables (see gems/omis-netsuite
NetsuiteStoreSalesOrder). This mirrors it.

WHY internal ids, not names: OMIS references item, taxCode, customer and class
purely by internal id (BaseReference internal_id). This account is set up for
ids, so the REST refs are {"id": ...}. transform_invoice carries human-readable
item/tax NAMES (built for the CSV path), so this module resolves those names to
internal ids via config/netsuite_customers.json:
  - item_ids:      {item name -> internal id}
  - tax_code_ids:  {tax code name -> internal id}
  - class_id / subsidiary_id: optional account-level refs (OMIS uses class "5")

Fill those ids in the config (look them up in NetSuite -- tools/netsuite_probe.py
can GET an existing OMIS sales order to read them off). Until an id is filled the
ref is emitted empty; unresolved_ids() reports which, and the push tool refuses a
live send while any remain.
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


def build_sales_order_payload(line_records: list[dict], refs: dict | None = None) -> dict:
    """One NetSuite REST salesOrder body from transform_invoice's line records.

    Refs are internal ids ({"id": ...}); an unresolved item/tax name emits an
    empty id (see unresolved_ids). Amounts and the customer id come straight from
    the mapped records -- only the item/tax/class refs are resolved here. Raises
    ValueError on an empty list.
    """
    if not line_records:
        raise ValueError("build_sales_order_payload needs at least one line record")
    refs = refs or load_refs()
    head = line_records[0]

    payload = {
        "externalId": head["external_id"],
        "entity": {"id": head["customer_id"]},
        "tranDate": head["tran_date"],
        "memo": head["memo"],
        "otherRefNum": head["other_ref_num"],
        "item": {
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
        },
    }
    # Optional account-level refs -- only sent when configured.
    if refs.get("class_id"):
        payload["class"] = {"id": refs["class_id"]}
    if refs.get("subsidiary_id"):
        payload["subsidiary"] = {"id": refs["subsidiary_id"]}
    return payload
