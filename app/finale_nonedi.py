"""
Finale invoices for NON-EDI sale orders -- HD Supply, Home Depot Special Orders, Chandos,
Cressey, and the OMIS commercial channels (HD Supply / HD Pro) once the OMIS->Finale
connector feeds them. These orders have no Crstl 850/810: their accounting runs elsewhere
(OMIS -> NetSuite), so the Finale invoice is an internal record only -- but without one the
order never completes and sits on the actionable sales-order list.

Classification is by EXCLUSION: a Finale sale order whose id is not a Crstl PO (and whose
saleSourceId is not an EDI source) is non-EDI. No customer list to maintain; the list grows
on its own.

    trigger   shipped in Finale (there is no 810 to wait for)
    lines     the order's own: unitPrice from the order line, qty = what SHIPPED
    discount  none
    tax       the customer party's postal-address province -> the same tax-rate products
    gate      province resolved + every shipped product priced by an order line -> post +
              complete the order; a shipped product the order doesn't price -> draft, flagged;
              no province / not shipped / already invoiced / completed -> skipped
    receipt   tracking key "order:<orderId>" (no Crstl transaction exists)

Same safety pattern as app.finale_invoice: dry run = the same read-only preflight, reporting
would-post / would-draft; idempotent on our receipt or any live invoice on the order; a
completed order is never touched (Finale refuses invoices on it anyway); per-run cap.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from app.netsuite import _load_config
from app.netsuite_payload import load_refs

EDI_SOURCES = {"HD Dropship", "HD DSD", "CRSTL"}
OPEN_STATUSES = {"ORDER_CREATED", "ORDER_LOCKED"}
INVOICE_TYPE = "SALES_INVOICE"
CANCELLED = "INVOICE_CANCELLED"
SKIPS = ("skipped_no_province", "skipped_no_map", "skipped_not_shipped", "skipped_exists",
         "skipped_no_order", "skipped_invalid")


def receipt_key(order_id) -> str:
    return f"order:{order_id}"


def is_non_edi(order: dict, crstl_pos) -> bool:
    oid = str(order.get("orderId") or "")
    return bool(oid) and oid not in crstl_pos and str(order.get("saleSourceId") or "") not in EDI_SOURCES


def select_candidates(orders: list[dict], crstl_pos, *, floor: str | None = None,
                      only=None, limit: int | None = None) -> list[dict]:
    """Open, non-EDI sale orders on/after the floor (Finale orderDate). Completed orders
    are excluded up front: Finale refuses a new invoice on them (403), and cancelled
    ones are gone."""
    wanted = {str(x) for x in only} if only is not None else None
    out = []
    for o in orders:
        if o.get("orderTypeId") not in (None, "SALES_ORDER"):
            continue
        if o.get("statusId") not in OPEN_STATUSES:
            continue
        if not is_non_edi(o, crstl_pos):
            continue
        if floor and str(o.get("orderDate") or "")[:10] < floor:
            continue
        if wanted is not None and str(o.get("orderId")) not in wanted:
            continue
        out.append(o)
    return out[:limit] if limit is not None else out


def province_tax(province, refs: dict, config: dict) -> dict:
    prov = str(province or "").upper()
    mapping = (config.get("dropship_provinces") or {}).get(prov) or {}
    fin = refs.get("finale") or {}
    tax_id = str((fin.get("tax_rate_ids") or {}).get(prov) or "")
    return {"province": prov, "tax_rate": mapping.get("tax_rate", 0), "tax_components": mapping.get("tax_components"),
            "tax_id": tax_id, "tax_desc": (fin.get("tax_desc") or {}).get(tax_id) or f"Tax {prov or '?'}"}


def customer_party_id(order: dict) -> str:
    roles = [r for r in (order.get("orderRoleList") or []) if r.get("roleTypeId") == "CUSTOMER"]
    if not roles:
        return ""
    return (str(roles[0].get("partyId") or "").strip()
            or str(roles[0].get("partyUrl") or "").rstrip("/").rsplit("/", 1)[-1])


def build_nonedi_invoice(order: dict, shipped: dict | None, province, refs: dict, account: str,
                         config: dict | None = None, today: str | None = None) -> dict:
    """The POST /api/invoice/ body for one shipped non-EDI order, or a skipped_* status."""
    config = config or _load_config()
    if not province:
        return {"status": "skipped_no_province"}
    t = province_tax(province, refs, config)
    if not t["tax_id"]:
        return {"status": "skipped_no_map", "province": t["province"]}
    if not shipped:
        return {"status": "skipped_not_shipped"}
    price = {str(l.get("productUrl") or ""): float(l.get("unitPrice") or 0)
             for l in (order.get("orderItemList") or []) if l.get("productUrl")}
    lines, unpriced, subtotal = [], [], 0.0
    for url, qty in shipped.items():
        if not qty or qty <= 0:
            continue
        if url not in price:
            unpriced.append(url.rsplit("/", 1)[-1])
            continue
        lines.append({"invoiceItemTypeId": "INV_PROD_ITEM", "productUrl": url,
                      "quantity": qty, "unitPrice": price[url]})
        subtotal += qty * price[url]
    if not lines:
        return {"status": "skipped_not_shipped"}
    subtotal = round(subtotal, 2)
    comps = t["tax_components"]
    tax = round(sum(round(subtotal * c, 2) for c in comps), 2) if comps else round(subtotal * (t["tax_rate"] or 0), 2)
    items = list(lines)
    if tax:
        items.append({"invoiceItemTypeId": "INV_SALES_TAX", "itemDescription": t["tax_desc"], "amount": tax,
                      "taxAuthorityRateProductUrl": f"/{account}/api/taxauthorityrateproduct/{t['tax_id']}"})
    day = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    body = {"invoiceUrl": None, "invoiceTypeId": INVOICE_TYPE,
            "primaryOrderUrl": order.get("orderUrl") or f"/{account}/api/order/{order.get('orderId')}",
            "invoiceDate": f"{day}T16:00:00.000Z", "invoiceItemList": items}
    return {"status": "built", "body": body, "province": t["province"], "gross": subtotal, "discount": 0.0,
            "net": subtotal, "tax": tax, "total": round(subtotal + tax, 2),
            "flag": ("shipped products the order does not price: " + ", ".join(unpriced)) if unpriced else None}


def push_nonedi_invoices(orders: list[dict], crstl_pos, *, live: bool = False, only=None,
                         limit: int | None = None, refs: dict | None = None, client=None,
                         province_index: dict | None = None, account: str | None = None,
                         floor: str | None = None, max_per_run: int | None = None,
                         today: str | None = None) -> dict:
    refs = refs or load_refs()
    config = _load_config()
    from app import tracking
    if client is None:
        from app.finale import FinaleClient, FinaleUnavailable
        if FinaleClient.configured():
            client = FinaleClient()
        elif live:
            raise FinaleUnavailable("FINALE_* credentials not set")
    if account is None:
        account = getattr(client, "account_id", None) or os.environ.get("FINALE_ACCOUNT_ID", "")
    if province_index is None:
        province_index = client.party_province_index() if client is not None else {}
    cands = select_candidates(orders, crstl_pos, floor=floor, only=only, limit=limit)
    mode = "live" if live else "dry"
    counts = {k: 0 for k in SKIPS}
    if max_per_run is not None and len(cands) > max_per_run:
        return {"mode": mode, "results": [], "blocked": f"{len(cands)} to invoice exceeds max_per_run {max_per_run}",
                "summary": {"candidates": len(cands), "built": 0, "posted": 0, "draft": 0, "failed": 0, **counts}}
    results: list[dict] = []
    posted = draft = failed = 0
    created: list[str] = []
    existing = tracking.get_finale_invoices([receipt_key(o.get("orderId")) for o in cands])
    for o in cands:
        oid = str(o.get("orderId")); key = receipt_key(oid)
        row = {"order_id": oid, "source": o.get("saleSourceId"), "customer": None, "status": None, "qty_flag": None}
        results.append(row)
        prior = existing.get(key)
        if prior and prior.get("status") in ("draft", "posted"):
            row.update(status="skipped_exists", invoice_id=prior.get("invoice_id"), invoice_id_user=prior.get("invoice_id_user"),
                       error=f"already invoiced ({prior.get('invoice_id_user') or prior.get('invoice_id')}, {prior.get('status')})")
            counts["skipped_exists"] += 1
            continue
        if client is None:
            row.update(status="skipped_invalid", error="Finale not configured"); counts["skipped_invalid"] += 1
            continue
        try:
            order = client.get_order(oid)
            if order is None:
                row.update(status="skipped_no_order", error=f"no Finale order {oid}"); counts["skipped_no_order"] += 1
                continue
            live_inv = [i for i in client.order_invoices(order) if i.get("statusId") != CANCELLED]
            if live_inv:
                row.update(status="skipped_exists", error="order already carries invoice "
                           + ", ".join(str(i.get("invoiceIdUser") or i.get("invoiceId")) for i in live_inv))
                counts["skipped_exists"] += 1
                continue
            pid = customer_party_id(order); row["customer"] = pid
            b = build_nonedi_invoice(order, client.shipment_qty_for_order(order), province_index.get(pid),
                                     refs, account, config, today=today)
            if b["status"] != "built":
                row["status"] = b["status"]; counts[b["status"]] += 1
                if b["status"] == "skipped_no_province":
                    row["error"] = f"customer party {pid or '?'} has no postal-address province"
                elif b["status"] == "skipped_not_shipped":
                    row["error"] = "not shipped in Finale yet -- will retry"
                continue
            row.update({k: v for k, v in b.items() if k not in ("body", "flag")}); row["qty_flag"] = b["flag"]
            clean = row["qty_flag"] is None
            if not live:
                row["status"] = "built"; row["would"] = "posted" if clean else "draft"
                continue
            made = client.create_invoice(b["body"])
            row["invoice_id"], row["invoice_url"], row["invoice_id_user"] = made.get("invoiceId"), made.get("invoiceUrl"), made.get("invoiceIdUser")
            if clean:
                client.complete_invoice(row["invoice_url"]); row["status"] = "posted"; posted += 1
                try:
                    row["order_completed"] = client.complete_order(order) is not None
                except Exception as exc:  # noqa: BLE001
                    row["order_completed"] = False; row["order_complete_error"] = str(exc)
            else:
                row["status"] = "draft"; draft += 1
            tracking.record_finale_invoice(key, oid, row["invoice_id"], row["invoice_url"], row["invoice_id_user"], row["status"])
            created.append(key)
        except Exception as exc:   # one bad order must not stop the batch
            row.update(status="failed", error=str(exc)); failed += 1
    if live and created:
        try:
            tracking.record_events(created, "finale")
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: non-EDI Finale invoices created but tracking failed: {exc}")
    return {"mode": mode, "results": results,
            "summary": {"candidates": len(cands), "built": sum(1 for r in results if r["status"] == "built"),
                        "posted": posted, "draft": draft, "failed": failed, **counts}}
