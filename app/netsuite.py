import functools
import json
import pathlib


@functools.lru_cache(maxsize=None)
def _load_config() -> dict:
    path = pathlib.Path(__file__).parent.parent / "config" / "netsuite_customers.json"
    return json.loads(path.read_text())


def transform_invoice(invoice: dict, province: str | None, store: str | None) -> list[dict] | None:
    """
    Transform a Crstl invoice into the NetSuite line-item records for one invoice.
    Returns None if the province/store has no mapping in config.

    Model (Backward Calculator V5 == HD Canada Invoice Allowance Spec):
      line 1 = merchandise at GROSS (Crstl `subtotal` = sum of qty*unit_price,
               i.e. before any SAC allowance);
      line 2 = one channel vendor-discount line, booked in NetSuite as a
               percentage Discount item that carries the rate on the item itself.
    We do NOT re-emit HD's per-SAC allowance lines: HD's 810 SAC data (and
    accounting's earlier NetSuite entries) were inconsistent -- the calculator is
    authoritative. The single effective off-gross rate already absorbs the
    MET-off-subtotal rule: DSD 6.19%, Dropship 5.19% (drops freight + RDC).

    Both records carry the province tax_code so the discount reduces the taxable
    base and NetSuite computes tax on the NET. `tax_amount` (informational; the
    REST path lets NetSuite recompute, the CSV path may too) is the net * the
    province rate, placed on the merchandise line, 0 on the discount line.

    `external_id` is the Crstl transaction_id -- the only field observed unique
    per invoice (PO numbers and invoice_numbers both collide). The human-friendly
    invoice_number rides in `memo` and (as the LEAD #) in the payload.
    """
    config = _load_config()

    if store is not None:
        mapping = config["dsd_stores"].get(store.upper())
        channel = "dsd"
    elif province is not None:
        mapping = config["dropship_provinces"].get(province.upper())
        channel = "dropship"
    else:
        return None

    if not mapping:
        return None

    discount = config["channel_discounts"][channel]

    try:
        base = {
            "external_id":    invoice["transaction_id"],
            "customer_id":    mapping["customer_id"],
            "tran_date":      invoice["invoice_date"],
            "due_date":       invoice["due_date"],
            "memo":           f'{invoice["invoice_number"]} / PO {invoice["po_number"]}',
            "invoice_number": invoice["invoice_number"],
            "other_ref_num":  invoice["po_number"],
            "currency":       config["currency"],
            "tax_code":       mapping["tax_code"],
        }

        gross = round(invoice["subtotal"], 2)
        # Discount amount is computed here so the total is deterministic and the
        # interim CSV path works; `rate` must match the NetSuite Discount item's
        # stored rate, so NetSuite recomputing it changes nothing.
        discount_amount = -round(gross * discount["rate"], 2)
        net = round(gross + discount_amount, 2)
        tax = round(net * mapping.get("tax_rate", 0), 2)

        return [
            {
                **base,
                "item":        config["item"],
                "description": "",
                "quantity":    1,
                "rate":        gross,
                "amount":      gross,
                "tax_amount":  tax,
            },
            {
                **base,
                "item":        discount["item"],
                "description": discount["note"],
                "quantity":    1,
                "rate":        discount_amount,
                "amount":      discount_amount,
                "tax_amount":  0,
            },
        ]
    except KeyError as exc:
        raise ValueError(f"Invoice missing required field: {exc}") from exc
