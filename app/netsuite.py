import functools
import json
import pathlib


@functools.lru_cache(maxsize=None)
def _load_config() -> dict:
    path = pathlib.Path(__file__).parent.parent / "config" / "netsuite_customers.json"
    return json.loads(path.read_text())


def external_id_for(invoice: dict) -> str:
    """Stable NetSuite externalId for a logical HD invoice.

    Keyed on CRSTL's `source_document_id`, NOT `transaction_id`. CRSTL issues a
    NEW transaction_id for every draft / resubmission / correction of the SAME
    invoice, but all of them share one source_document_id (verified: 235 rows =>
    176 source docs, never spanning two POs). Keying on it means a re-pushed
    correction UPDATES the same NetSuite invoice instead of creating a duplicate
    -- the old transaction_id key created one NetSuite record per version.

    The `CRSTL-` prefix namespaces it as ours: invoices are entered into this
    NetSuite account from several sources, and our upsert (PUT .../eid:{id}) can
    only ever match a record WE created. Another source's manual invoice carries
    no such externalId, so we can never overwrite it by accident.
    """
    sid = str(invoice.get("source_document_id") or "").strip()
    if not sid:
        raise ValueError("invoice missing source_document_id (needed for a stable NetSuite externalId)")
    return f"CRSTL-{sid}"


def resolve_customer(invoice: dict, province, store, config: dict | None = None) -> dict | None:
    """The NetSuite customer + channel this invoice routes to, or None if it can't
    be routed (no province/store mapping, or a Mixed/Unknown dropship product).

    SINGLE source of truth for customer routing, used by BOTH transform_invoice
    (the booking) and the dashboard (which shows the target so it matches exactly
    what will post). DSD is drapery only -> its store customer. Dropship splits by
    PRODUCT: a blind bills to the province's blinds customer, a drape panel to the
    existing (panels) customer. Tax always follows the province, not the product.
    """
    config = config or _load_config()
    if store is not None:
        mapping = config["dsd_stores"].get(store.upper()); channel = "dsd"
    elif province is not None:
        mapping = config["dropship_provinces"].get(province.upper()); channel = "dropship"
    else:
        return None
    if not mapping:
        return None
    customer_id = mapping["customer_id"]
    product = invoice.get("product")
    if channel == "dropship":
        if product == "Blind":
            customer_id = (config.get("dropship_blinds") or {}).get(province.upper())
            if not customer_id:
                return None
        elif product != "Drape Panel":
            return None
    return {
        "channel": channel,
        "product": product,
        "customer_id": customer_id,
        "customer_name": (config.get("customer_names") or {}).get(str(customer_id)),
        "tax_code": mapping["tax_code"],
        "tax_rate": mapping.get("tax_rate", 0),
    }


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

    `external_id` is derived from the Crstl `source_document_id` (see
    external_id_for): stable across a logical invoice's drafts and resubmissions,
    so a re-push updates rather than duplicates. The human-friendly invoice_number
    rides in `memo` and (as the LEAD #) in the payload.
    """
    config = _load_config()

    route = resolve_customer(invoice, province, store, config)
    if route is None:
        return None
    channel = route["channel"]
    discount = config["channel_discounts"][channel]

    try:
        base = {
            "external_id":    external_id_for(invoice),
            "customer_id":    route["customer_id"],
            "customer_name":  route["customer_name"],
            "tran_date":      invoice["invoice_date"],
            "due_date":       invoice["due_date"],
            "memo":           f'{invoice["invoice_number"]} / PO {invoice["po_number"]}',
            "invoice_number": invoice["invoice_number"],
            "other_ref_num":  invoice["po_number"],
            "currency":       config["currency"],
            "tax_code":       route["tax_code"],
        }

        gross = round(invoice["subtotal"], 2)
        # Discount amount is computed here so the total is deterministic and the
        # interim CSV path works; `rate` must match the NetSuite Discount item's
        # stored rate, so NetSuite recomputing it changes nothing.
        discount_amount = -round(gross * discount["rate"], 2)
        net = round(gross + discount_amount, 2)
        tax = round(net * route["tax_rate"], 2)

        return [
            {
                **base,
                "item":        (config.get("product_items") or {}).get(route["product"]) or config["item"],
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
