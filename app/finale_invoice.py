"""
Finale invoice creation from the CRSTL EDI -- one engine for dry run + live, mirroring
app.netsuite_push. ONE tool invoices BOTH channels (DSD + Dropship); Finale invoices
are internal records for teams without NetSuite access and go nowhere downstream.

EDI-driven, single source of truth (the same 810 that feeds HD and the NetSuite SO):
    product     850 line `stock_keeping_unit` (HD item #) -> Finale productId -> productUrl,
                UPC as the fallback key; joined to the 810 on line_item_number
    qty / price 810 line quantity_invoiced / unit_price (the 810 is raised after
                shipping, so quantity_invoiced IS the shipped qty)
    discount    -(allowance_amount + discount_amount): CRSTL's exact reduction, as one
                INV_PROMOTION_ADJ on the channel preset (DSD 100037 / Dropship 100038)
    tax         net x province rate (compound provinces round each component), as one
                INV_SALES_TAX on the province's tax-rate product

Gate: the invoice is POSTED (/complete) only when it ties to the CRSTL 810 total to
the cent AND Finale's shipped qty matches the 810; a mismatch is created as a DRAFT
and flagged for a human (surfaced, never silently invoiced). An order NOT YET SHIPPED
in Finale is skipped -- no invoice, no receipt -- so the next poll retries it once the
shipment exists (manual or integration). A posted invoice then COMPLETES the order,
which is what takes it off the actionable sales-order list.

Safety (see app.netsuite_push for the pattern):
  * dry run builds and reconciles but writes nothing;
  * a live run is REFUSED while any promo/tax-rate id is unresolved in config;
  * idempotent: a transaction with a Finale receipt, or an order that already
    carries a non-cancelled invoice, is skipped -- Finale's collection POST always
    creates, so the guard has to live here;
  * every created invoice (draft or posted) records a 'finale' event + its id.
"""
from __future__ import annotations

from app.netsuite import _load_config, resolve_customer
from app.netsuite_payload import load_refs
from app.netsuite_push import _select, eligible_for_push
from app.finale import API_LOGIN, MOVED, adoptable_draft, approved_by, carrier_fix, created_by, invoice_total, wanted_carrier

# An order in one of these states is still open: nothing to reopen, nothing to skip.
OPEN_ORDERS = ("ORDER_CREATED", "ORDER_LOCKED")

INVOICE_TYPE = "SALES_INVOICE"
CANCELLED = "INVOICE_CANCELLED"


def resolve_finale_refs(invoice: dict, refs: dict, config: dict | None = None) -> dict | None:
    """Channel, province, tax rate/components, and the Finale preset + tax-rate ids
    this invoice books to -- or None if it can't be routed (same routing as NetSuite:
    resolve_customer). Missing ids come back blank and are reported as unresolved."""
    config = config or _load_config()
    store, province = invoice.get("store"), invoice.get("province")
    route = resolve_customer(invoice, province, store, config)
    if route is None:
        return None
    fin = refs.get("finale") or {}
    channel = route["channel"]
    if channel == "dsd":
        tax_prov = (config.get("dsd_stores", {}).get(str(store).upper()) or {}).get("province") or ""
    else:
        tax_prov = province or ""
    tax_prov = str(tax_prov).upper()
    promo_id = str((fin.get("promo_ids") or {}).get(channel) or "")
    tax_id = str((fin.get("tax_rate_ids") or {}).get(tax_prov) or "")
    return {
        "channel": channel,
        "province": tax_prov,
        "tax_rate": route.get("tax_rate", 0),
        "tax_components": route.get("tax_components"),
        "promo_id": promo_id,
        "promo_desc": (fin.get("promo_desc") or {}).get(channel) or f"{channel} discount",
        "tax_id": tax_id,
        "tax_desc": (fin.get("tax_desc") or {}).get(tax_id) or f"Tax {tax_prov or '?'}",
    }


def unresolved_finale_ids(r: dict) -> list[str]:
    tags = []
    if not r.get("promo_id"):
        tags.append(f"promo:{r.get('channel')}")
    if not r.get("tax_id"):
        tags.append(f"taxrate:{r.get('province') or '?'}")
    return tags


def money(invoice: dict, r: dict, config: dict) -> dict:
    """gross -> exact CRSTL reduction -> net -> tax -> total, and the delta vs the
    810 total. Same numbers the NetSuite SO books (app.netsuite.transform_invoice)."""
    gross = round(float(invoice["subtotal"]), 2)
    reduction = round((invoice.get("allowance_amount") or 0) + (invoice.get("discount_amount") or 0), 2)
    if reduction <= 0:   # CRSTL reported none (unexpected for Accepted): flat channel rate
        reduction = round(gross * config["channel_discounts"][r["channel"]]["rate"], 2)
    net = round(gross - reduction, 2)
    comps = r.get("tax_components")
    tax = round(sum(round(net * c, 2) for c in comps), 2) if comps else round(net * (r.get("tax_rate") or 0), 2)
    total = round(net + tax, 2)
    hd_total = invoice.get("total_amount")
    delta = None if hd_total is None else round(total - float(hd_total), 2)
    return {"gross": gross, "discount": -reduction, "net": net, "tax": tax, "total": total,
            "hd_total": hd_total, "delta": delta}


def build_product_lines(invoice: dict, po_lines: list[dict] | None, product_index: dict) -> tuple[list[dict], list[str]]:
    """(INV_PROD_ITEM lines, 810 line numbers whose product could not be resolved).
    Product identity comes from the 850 line with the same line_item_number; qty and
    unit price from the 810 line. A line whose product isn't in the catalogue is
    reported, never guessed."""
    by_num = {str(l.get("line_item_number") or ""): l for l in (po_lines or [])}
    lines, missing = [], []
    for l in invoice.get("invoice_lines") or []:
        num = str(l.get("line_item_number") or "")
        po = by_num.get(num) or {}
        url = (product_index.get(po.get("sku") or "") or product_index.get(po.get("upc") or "")
               or product_index.get(str(l.get("upc") or "")))
        if not url:
            missing.append(num or "?")
            continue
        lines.append({
            "invoiceItemTypeId": "INV_PROD_ITEM",
            "productUrl": url,
            "quantity": l.get("quantity"),
            "unitPrice": l.get("unit_price"),
            "itemDescription": po.get("vendor_item") or po.get("sku") or str(l.get("description") or ""),
        })
    return lines, missing


def build_finale_invoice(invoice: dict, po_entry: dict | None, product_index: dict,
                         refs: dict, account: str, config: dict | None = None) -> dict:
    """The POST /api/invoice/ body for one Crstl invoice plus its reconciliation.
    status: built | skipped_no_map | skipped_no_po."""
    config = config or _load_config()
    r = resolve_finale_refs(invoice, refs, config)
    if r is None:
        return {"status": "skipped_no_map"}
    if not (po_entry or {}).get("lines"):
        return {"status": "skipped_no_po", "channel": r["channel"]}
    m = money(invoice, r, config)
    prod_lines, missing = build_product_lines(invoice, po_entry["lines"], product_index)
    items = list(prod_lines)
    if m["discount"]:
        items.append({"invoiceItemTypeId": "INV_PROMOTION_ADJ", "itemDescription": r["promo_desc"],
                      "amount": m["discount"],
                      "productPromoUrl": f"/{account}/api/productpromo/{r['promo_id']}"})
    if m["tax"]:
        items.append({"invoiceItemTypeId": "INV_SALES_TAX", "itemDescription": r["tax_desc"],
                      "amount": m["tax"],
                      "taxAuthorityRateProductUrl": f"/{account}/api/taxauthorityrateproduct/{r['tax_id']}"})
    body = {
        "invoiceUrl": None,
        "invoiceTypeId": INVOICE_TYPE,
        "primaryOrderUrl": f"/{account}/api/order/{invoice.get('po_number')}",
        "invoiceDate": f"{str(invoice.get('invoice_date') or '')[:10]}T16:00:00.000Z",
        "invoiceItemList": items,
    }
    if invoice.get("invoice_number"):
        # What the warehouse keys by hand on every DSD invoice (40864264-1: "INV40864264").
        body["referenceNumber"] = str(invoice["invoice_number"])
    d = m["delta"]
    return {
        "status": "built", "body": body, "channel": r["channel"],
        "where": invoice.get("store") or r["province"], **m,
        "reconcile_flag": None if (d is None or abs(d) <= 0.01) else f"total off CRSTL 810 by {d:+.2f}",
        "missing_products": missing,
        "unresolved": unresolved_finale_ids(r),
    }


def external_view(live_invoices: list[dict], hd_total) -> dict:
    """What is already on the order, whoever keyed it: ids, creator (of the first),
    posted-or-draft, the summed total of every live invoice and its delta vs the
    810 total. The digest's 'does Finale tie to the 810' answer for invoices we
    did not create."""
    first = live_invoices[0]
    total = round(sum(invoice_total(i) for i in live_invoices), 2)
    posted = all(i.get("statusId") == "INVOICE_APPROVED" for i in live_invoices)
    return {
        "invoice_id": first.get("invoiceId"),
        "invoice_url": first.get("invoiceUrl"),
        "invoice_id_user": ", ".join(str(i.get("invoiceIdUser") or i.get("invoiceId")) for i in live_invoices),
        "created_by": created_by(first) or approved_by(first),
        "finale_status": "posted" if posted else "draft",
        "finale_total": total,
        "delta": None if hd_total is None else round(total - float(hd_total), 2),
    }


def qty_check(prod_lines: list[dict], shipped: dict | None) -> tuple[list[str], bool]:
    """(mismatch messages, verified). Unverified (no moved shipment) is reported
    separately from a mismatch so the caller can hold the invoice without calling
    a not-yet-shipped order a discrepancy."""
    if shipped is None:
        return [], False
    invoiced: dict = {}
    for l in prod_lines:
        invoiced[l["productUrl"]] = invoiced.get(l["productUrl"], 0.0) + float(l.get("quantity") or 0)
    out = []
    for url, q in invoiced.items():
        s = shipped.get(url, 0.0)
        if abs(q - s) > 1e-6:
            out.append(f"{url.rsplit('/', 1)[-1]}: invoiced {q:g} vs shipped {s:g}")
    return out, True


def push_finale_invoices(
    invoices: list[dict],
    po_map: dict[str, dict],
    *,
    live: bool = False,
    only: list[str] | None = None,
    limit: int | None = None,
    refs: dict | None = None,
    client=None,
    product_index: dict | None = None,
    account: str | None = None,
    auto_reopen: bool | None = None,
    max_per_run: int | None = None,
    shipped_pos: set | None = None,
    order_status: dict | None = None,
) -> dict:
    """Build (and, when live, create + post) Finale invoices for these Crstl invoices.

    Returns {mode, unresolved, results, summary, blocked?}. results: one row per
    invoice -- status built | posted | draft | failed | skipped_*, the money view,
    reconcile_flag, qty_flag, missing_products, and the Finale invoice id/url.

    `max_per_run` is the automation blast cap and counts INVOICES ABOUT TO BE
    CREATED -- the rows that survive preflight -- not the pending set: an Accepted
    810 whose order has not shipped yet is waiting, not writing, and must never
    push the poll into refusing. Over the cap the whole run is refused (nothing
    written, `blocked` set); it is never truncated. Manual runs pass None.

    `shipped_pos` (POs with a shipped/delivered shipment) and `order_status` ({po:
    statusId}) come from one shipment listing and one sale-order listing, and let the
    15-min poll skip its reads for an order with nothing shipped yet that is still
    open -- those reads could only end in skipped_not_shipped, and re-reading every
    waiting order every poll ran it past Finale's 120 reads/minute (2026-09-21). An
    order that is completed or cancelled, or that the listing does not know, gets the
    full check as before (reopen, cancelled). None = read every order (manual runs,
    the NetSuite ride-along, the digest's reconciliation).
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    refs = refs or load_refs()
    config = _load_config()
    if auto_reopen is None:
        auto_reopen = bool((refs.get("finale") or {}).get("auto_reopen"))
    if account is None:
        account = getattr(client, "account_id", None) or __import__("os").environ.get("FINALE_ACCOUNT_ID", "")
    if product_index is None:
        if client is not None:
            product_index = client.product_index()
        else:
            # Dry run with no client still needs the catalogue to resolve products
            # (read-only GET) -- otherwise the preview reports every line as missing.
            from app.finale import FinaleClient
            product_index = FinaleClient().product_index() if FinaleClient.configured() else {}
    invoices = _select(eligible_for_push(invoices), only, limit)

    results: list[dict] = []
    prepared: list[tuple[dict, dict]] = []
    unresolved: list[str] = []
    counts = {k: 0 for k in ("skipped_no_map", "skipped_no_po", "skipped_no_product",
                             "skipped_invalid", "skipped_exists", "skipped_no_order",
                             "skipped_not_shipped", "skipped_completed")}

    for inv in invoices:
        tid = str(inv.get("transaction_id", "?"))
        base = {"transaction_id": tid, "invoice_number": inv.get("invoice_number"),
                "po_number": inv.get("po_number")}
        try:
            b = build_finale_invoice(inv, po_map.get(str(inv.get("po_number") or "")), product_index,
                                     refs, account, config)
        except (KeyError, TypeError, ValueError) as exc:
            counts["skipped_invalid"] += 1
            results.append({**base, "channel": None, "where": None, "status": "skipped_invalid", "error": str(exc)})
            continue
        if b["status"] != "built":
            counts[b["status"]] += 1
            results.append({**base, "channel": b.get("channel"), "where": None, "status": b["status"]})
            continue
        row = {**base, **{k: v for k, v in b.items() if k != "body"}}
        row["qty_flag"] = None
        if b["missing_products"]:
            row["status"] = "skipped_no_product"
            row["error"] = "no Finale product for 810 line(s) " + ", ".join(b["missing_products"])
            counts["skipped_no_product"] += 1
            results.append(row)
            continue
        for tag in b["unresolved"]:
            if tag not in unresolved:
                unresolved.append(tag)
        results.append(row)
        prepared.append((row, b["body"]))

    mode = "live" if live else "dry"
    posted = draft = failed = 0

    # Carrier defaults per EDI channel (config finale.carriers, OFF by default): set on
    # the shipped shipment(s) inside the reopen the engine already does -- dropship's
    # ShipStation connection ships with no carrier; DSD's is a fallback for a pickup
    # the DSD pass did not reach. Non-EDI channels never get one (wanted_carrier).
    fin_cfg = refs.get("finale") or {}
    carrier_cfg = fin_cfg.get("carriers") or {}
    carrier_index: dict | None = None
    carrier_error: str | None = None     # one failed listing is reported as such on EVERY row -- never as "not on the list"

    def carrier_for(channel: str) -> dict:
        nonlocal carrier_index, carrier_error
        if not carrier_cfg.get(channel) or client is None:
            return wanted_carrier(fin_cfg, channel, {})
        if carrier_index is None and carrier_error is None:
            try:
                carrier_index = client.carrier_index()
            except Exception as exc:  # noqa: BLE001 -- invoicing goes on; the carrier does not
                carrier_error = f"could not read Finale's carriers: {exc}"
        if carrier_error:
            return {**wanted_carrier(fin_cfg, channel, {}), "url": None, "reason": carrier_error}
        return wanted_carrier(fin_cfg, channel, carrier_index)

    def preflight(client, row, body, prior):
        """The read-only checks that decide whether a row may be written, identical
        for a dry run and a live run: our own receipt, the order's existence, an
        invoice already on the order, and the shipped-qty gate. Sets a skipped_*
        status and returns None when the row must not be written; otherwise sets
        qty_flag and returns the order."""
        tid = row["transaction_id"]
        # Our own receipt short-circuits. An 'external' receipt (someone else's
        # invoice) does NOT: the live invoices are re-read below, so a hand-made
        # invoice that was since cancelled lets the order be invoiced properly.
        if prior and prior.get("status") in ("draft", "posted"):
            row["status"] = "skipped_exists"
            row["invoice_id"], row["invoice_url"], row["invoice_id_user"] = prior.get("invoice_id"), prior.get("invoice_url"), prior.get("invoice_id_user")
            row["error"] = f"already invoiced in Finale ({prior.get('invoice_id_user') or prior.get('invoice_id')}, {prior.get('status')})"
            counts["skipped_exists"] += 1
            return None
        po = str(row["po_number"])
        if shipped_pos is not None and po not in shipped_pos and (order_status or {}).get(po) in OPEN_ORDERS:
            row["status"] = "skipped_not_shipped"
            row["error"] = "not shipped in Finale yet -- will retry"
            counts["skipped_not_shipped"] += 1
            return None
        order = client.get_order(po)
        if order is None:
            row["status"] = "skipped_no_order"
            row["error"] = f"no Finale order {row['po_number']}"
            counts["skipped_no_order"] += 1
            return None
        if order.get("statusId") == "ORDER_CANCELLED":
            row["status"] = "skipped_completed"
            row["error"] = "Finale order is cancelled"
            counts["skipped_completed"] += 1
            return None
        live_invoices = [i for i in client.order_invoices(order) if i.get("statusId") != CANCELLED]
        if order.get("statusId") == "ORDER_COMPLETED" and not live_invoices:
            # Completed with no invoice: the ShipStation connection completed it on the
            # ship event without its shipment step. Finale would 403 an invoice, so by
            # Ritchie's rule it is NOT complete -- reopen it (edit -> lock) and carry on:
            # shipped -> invoice + re-complete; unshipped -> stays open for the warehouse.
            if not auto_reopen:
                row["status"] = "skipped_completed"
                row["would_reopen"] = True
                row["error"] = "completed with no shipment/invoice -- reopen it to invoice (auto_reopen off)"
                counts["skipped_completed"] += 1
                return None
            if live:
                order = client.reopen_order(order)
                row["reopened"] = True
            else:
                # Dry: the shipped-qty check below is read-only, so the preview can
                # still say posted/draft; it just notes the reopen the live run does.
                row["would_reopen"] = True
        if live_invoices:
            orphan = adoptable_draft(live_invoices)
            if orphan is None:
                # Someone else already invoiced this order (by hand, or another
                # integration). Not an error and not a gap: read what is there -- who,
                # how much, and the delta vs the 810 -- so the digest can reconcile it.
                # A live run receipts it as 'external' (+ the finale event) so the poll
                # stops re-reading it every 15 minutes; a dry run only reports.
                row["status"] = "skipped_exists"
                row["error"] = ("order already carries invoice "
                                + ", ".join(str(i.get("invoiceIdUser") or i.get("invoiceId")) for i in live_invoices))
                counts["skipped_exists"] += 1
                row["external"] = external_view(live_invoices, row.get("hd_total"))
                if live:
                    ext = row["external"]
                    tracking.record_finale_invoice(tid, str(row["po_number"]), ext["invoice_id"], ext["invoice_url"],
                                                   ext["invoice_id_user"], "external", created_by=ext["created_by"],
                                                   finale_total=ext["finale_total"], delta=ext["delta"])
                    tracking.record_push_snapshot(tid, row.get("invoice_number"), "finale", row.get("hd_total"))
                    external_ids.append(tid)
                return None
            # A lone un-posted draft with no receipt: ours from a half-failed run, or
            # keyed by hand. Adopt it instead of creating a second one -- posted below
            # when it is ours and the build is clean, else receipted as a draft to review.
            row["adopt"] = {"invoice_id": orphan.get("invoiceId"), "invoice_url": orphan.get("invoiceUrl"),
                            "invoice_id_user": orphan.get("invoiceIdUser"), "ours": created_by(orphan) == API_LOGIN,
                            "record": orphan}
            row["adopted"] = orphan.get("invoiceIdUser") or orphan.get("invoiceId")
        mism, verified = qty_check([l for l in body["invoiceItemList"] if l["invoiceItemTypeId"] == "INV_PROD_ITEM"],
                                   client.shipment_qty_for_order(order))
        if not verified:
            # Not shipped in Finale yet: leave it alone (no invoice, no receipt)
            # so the next poll picks it up once the shipment exists.
            row["status"] = "skipped_not_shipped"
            row["error"] = "not shipped in Finale yet -- will retry"
            counts["skipped_not_shipped"] += 1
            return None
        if mism:
            row["qty_flag"] = "shipped qty != 810: " + "; ".join(mism)
        want = carrier_for(str(row.get("channel") or ""))
        if want["name"]:
            fix = carrier_fix(client.order_shipments(order), want["url"], MOVED) if want["url"] else []
            row["carrier"] = {"wanted": want["name"], "enabled": want["enabled"],
                              "shipments": [str(s.get("shipmentIdUser") or s.get("shipmentId")) for s in fix],
                              "note": want["reason"] or None}
            row["_carrier_fix"] = [(str(s.get("shipmentUrl")), want["url"]) for s in fix] if want["enabled"] else []
        return order

    from app import tracking
    if client is None:
        from app.finale import FinaleClient, FinaleUnavailable
        if FinaleClient.configured():
            client = FinaleClient()
        elif live:
            raise FinaleUnavailable("FINALE_* credentials not set")

    if live and unresolved:
        return {"mode": mode, "unresolved": unresolved, "results": results, "blocked": "unresolved ids",
                "summary": {"built": len(prepared), "posted": 0, "draft": 0, "failed": 0, **counts}}

    blocked = None
    if client is not None:
        # Read-only preflight for BOTH modes, so a dry run reports exactly what a
        # live run would do (skip / retry / would-post / would-draft) -- the
        # "prove the first run" preview must not overstate.
        existing = tracking.get_finale_invoices([r["transaction_id"] for r, _ in prepared])
        created_ids: list[str] = []
        external_ids: list[str] = []
        writers: list[tuple[dict, dict, dict, bool]] = []
        for row, body in prepared:
            tid = row["transaction_id"]
            try:
                order = preflight(client, row, body, existing.get(tid))
            except Exception as exc:
                row["status"] = "failed"; row["error"] = str(exc); failed += 1
                continue
            if order is None:
                continue
            clean = row["reconcile_flag"] is None and row["qty_flag"] is None
            adopt = row.get("adopt")
            postable = clean and (adopt is None or adopt["ours"])   # never post a draft someone else keyed
            row["would"] = "posted" if postable else "draft"
            writers.append((row, body, order, postable))
        # The cap counts what is about to be WRITTEN. Checked before the first
        # create so a refusal leaves Finale exactly as it was.
        if max_per_run is not None and len(writers) > max_per_run:
            blocked = f"{len(writers)} invoices to create exceeds max_per_run {max_per_run} -- nothing written"
        for row, body, order, clean in (writers if (live and not blocked) else []):
            tid = row["transaction_id"]
            row.pop("would", None)
            # The carrier goes on first, while the order is open (reopened above if
            # it was completed). A failure here is reported on the row and never
            # stops the invoice.
            for surl, curl in row.pop("_carrier_fix", []):
                try:
                    client.update_shipment(surl, {"carrierPartyUrl": curl})
                    row.setdefault("carrier_set", []).append(surl.rsplit("/", 1)[-1])
                except Exception as exc:  # noqa: BLE001
                    row["carrier_error"] = str(exc)
            try:
                adopt = row.pop("adopt", None)
                adopt_rec = adopt.get("record") if adopt else None
                if adopt:
                    row["invoice_id"], row["invoice_url"], row["invoice_id_user"] = adopt["invoice_id"], adopt["invoice_url"], adopt["invoice_id_user"]
                else:
                    created = client.create_invoice(body)
                    row["invoice_id"] = created.get("invoiceId")
                    row["invoice_url"] = created.get("invoiceUrl")
                    row["invoice_id_user"] = created.get("invoiceIdUser")
                if clean:
                    client.complete_invoice(row["invoice_url"])
                    row["status"] = "posted"; posted += 1
                    # Declutter: a posted invoice completes its order. Best-effort --
                    # the invoice is already posted, so a failure here is reported,
                    # never turned into a failed invoice.
                    try:
                        row["order_completed"] = client.complete_order(order) is not None
                    except Exception as exc:  # noqa: BLE001
                        row["order_completed"] = False
                        row["order_complete_error"] = str(exc)
                else:
                    row["status"] = "draft"; draft += 1
                tracking.record_finale_invoice(tid, str(row["po_number"]), row["invoice_id"], row["invoice_url"],
                                               row["invoice_id_user"], row["status"],
                                               created_by=(created_by(adopt_rec) if adopt_rec is not None else API_LOGIN),
                                               finale_total=row.get("total"), delta=row.get("delta"))
                tracking.record_push_snapshot(tid, row.get("invoice_number"), "finale", row.get("hd_total"))
                created_ids.append(tid)
            except Exception as exc:   # one bad invoice must not stop the batch
                row["status"] = "failed"
                row["error"] = str(exc)
                failed += 1
        if live and (created_ids or external_ids):
            try:
                tracking.record_events(created_ids + external_ids, "finale")
            except Exception as exc:  # noqa: BLE001
                print(f"WARNING: finale invoices created but tracking failed: {exc}")

    for r in results:
        r.pop("_carrier_fix", None)
    built = sum(1 for r in results if r.get("status") == "built")
    out = {"mode": mode, "unresolved": unresolved, "results": results,
           "summary": {"built": built, "posted": posted, "draft": draft, "failed": failed, **counts}}
    if blocked:
        out["blocked"] = blocked
    return out
