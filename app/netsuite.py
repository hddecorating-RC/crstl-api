import json
import pathlib


def _load_config() -> dict:
    path = pathlib.Path(__file__).parent.parent / "config" / "netsuite_customers.json"
    return json.loads(path.read_text())


def transform_invoice(invoice: dict, province: str | None, store: str | None) -> dict | None:
    """
    Transform a Crstl invoice dict into a NetSuite record shape.
    Returns None if the province/store has no mapping in config.

    store: uppercase city key for wholesale ("VAUGHAN", "CALGARY"), or None for dropship.
    province: 2-letter Canadian province code ("ON", "QC", etc.), used when store is None.
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

    return {
        "external_id":   invoice["po_number"],
        "customer_id":   mapping["customer_id"],
        "tran_date":     invoice["invoice_date"],
        "due_date":      invoice["due_date"],
        "memo":          invoice["po_number"],
        "other_ref_num": invoice["po_number"],
        "currency":      config["currency"],
        "item":          config["item"],
        "quantity":      1,
        "rate":          invoice["subtotal"],
        "amount":        invoice["subtotal"],
        "tax_code":      mapping["tax_code"],
        "tax_amount":    invoice["tax_amount"],
        "total_amount":  invoice["total_amount"],
    }
