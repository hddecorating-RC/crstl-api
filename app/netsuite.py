import functools
import json
import pathlib


@functools.lru_cache(maxsize=None)
def _load_config() -> dict:
    path = pathlib.Path(__file__).parent.parent / "config" / "netsuite_customers.json"
    return json.loads(path.read_text())


def transform_invoice(invoice: dict, province: str | None, store: str | None) -> list[dict] | None:
    """
    Transform a Crstl invoice into a list of NetSuite line-item records that
    together represent one invoice. Returns None if the province/store has no
    mapping in config.

    Output shape: one record per line, all sharing the same `external_id`. The
    first record is the merchandise line (subtotal); one additional record is
    emitted per SAC allowance (negative rate) or charge (positive rate) so the
    NetSuite invoice total matches HD's actual invoiced total.

    `external_id` is the Crstl transaction_id — the only field observed to be
    unique per invoice. PO numbers collide across corrections/split shipments;
    invoice_numbers are also reused by HD across distinct invoices (measured:
    4 collisions across 91 invoices). The human-friendly invoice_number is
    kept in `memo` so accounting can still search by it in NetSuite.

    `tax_amount` is placed on the merchandise line only; allowance/charge
    lines carry 0 so that summing per-line tax on import gives HD's invoice
    total tax exactly. If NetSuite is configured to recompute tax from the
    tax code, the per-line amounts are ignored — but the tax code applied to
    every line ensures each line contributes to the taxable base correctly.
    """
    config = _load_config()

    if store is not None:
        mapping = config["wholesale_stores"].get(store.upper())
    elif province is not None:
        mapping = config["dropship_provinces"].get(province.upper())
    else:
        return None

    if not mapping:
        return None

    try:
        base = {
            "external_id":   invoice["transaction_id"],
            "customer_id":   mapping["customer_id"],
            "tran_date":     invoice["invoice_date"],
            "due_date":      invoice["due_date"],
            "memo":          f'{invoice["invoice_number"]} / PO {invoice["po_number"]}',
            "other_ref_num": invoice["po_number"],
            "currency":      config["currency"],
            "tax_code":      mapping["tax_code"],
        }

        records = [{
            **base,
            "item":         config["item"],
            "description":  "",
            "quantity":     1,
            "rate":         invoice["subtotal"],
            "amount":       invoice["subtotal"],
            "tax_amount":   invoice["tax_amount"],
        }]

        for entry in invoice.get("allowances_charges") or []:
            amt = entry.get("amount", 0)
            is_allowance = entry.get("type") == "Allowance"
            signed_rate = -amt if is_allowance else amt
            records.append({
                **base,
                "item":         config["allowance_item"] if is_allowance else config["charge_item"],
                "description":  entry.get("code", ""),
                "quantity":     1,
                "rate":         signed_rate,
                "amount":       signed_rate,
                "tax_amount":   0,
            })

        return records
    except KeyError as exc:
        raise ValueError(f"Invoice missing required field: {exc}") from exc
