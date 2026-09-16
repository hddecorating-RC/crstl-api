"""ShipStation: close DSD orders once Finale has shipped them.

DSD pickups are shipped by HD's own trucks, so their ShipStation orders (store
"HD DSD") never get a label and sit in Awaiting Shipment forever. The warehouse's
Ship click in Finale is the real event; this pass mirrors it into ShipStation with
"Mark as Shipped" -- carrier Other, the PRO as tracking, Finale's ship date, no
notifications. Mark as Shipped does NOT fire the SHIP_NOTIFY webhook that CRSTL
builds ASNs from (proven 2026-09-16 on 31 orders: zero new 856s), so this never
touches the EDI side.

Guards (Ritchie, 2026-09-16): the DSD store only; a positive floor on the
ShipStation order's create date so history is never swept; Finale must show the
shipment SHIPPED (packed/open = wait, cancelled = report); one receipt per order;
a blast cap; ships OFF.
"""
import os
from datetime import datetime, timezone

import requests

OPEN_STATUSES = ("awaiting_payment", "awaiting_shipment", "pending_fulfillment", "on_hold")
MOVED = ("SHIPMENT_SHIPPED", "SHIPMENT_DELIVERED")


class ShipStationClient:
    BASE = "https://ssapi.shipstation.com"
    TIMEOUT = 30

    @staticmethod
    def configured() -> bool:
        return bool(os.environ.get("SHIPSTATION_V1_KEY") and os.environ.get("SHIPSTATION_V1_SECRET"))

    def __init__(self):
        self.session = requests.Session()
        self.session.auth = (os.environ.get("SHIPSTATION_V1_KEY", ""), os.environ.get("SHIPSTATION_V1_SECRET", ""))

    def _get(self, path: str, **params) -> dict:
        r = self.session.get(self.BASE + path, params=params, timeout=self.TIMEOUT)
        r.raise_for_status()
        return r.json()

    def list_open_orders(self, store_id: int) -> list[dict]:
        """Every not-yet-shipped, not-cancelled order in this store (all pages)."""
        out: list[dict] = []
        for status in OPEN_STATUSES:
            page = 1
            while True:
                res = self._get("/orders", storeId=store_id, orderStatus=status, pageSize=500, page=page)
                out += res.get("orders") or []
                if page >= int(res.get("pages") or 1):
                    break
                page += 1
        return out

    def orders_by_number(self, order_number: str) -> list[dict]:
        return self._get("/orders", orderNumber=order_number, pageSize=50).get("orders") or []

    def mark_as_shipped(self, order_id: int, fields: dict) -> dict:
        """POST /orders/markasshipped -- no label, no notifications. The only write."""
        body = {"orderId": order_id, "notifyCustomer": False, "notifySalesChannel": False, **fields}
        r = self.session.post(self.BASE + "/orders/markasshipped", json=body, timeout=self.TIMEOUT)
        r.raise_for_status()
        return r.json()


def plan_close(ss_order: dict, finale_order: dict | None, shipments: list[dict], *,
               created_after: str | None, carrier_code: str = "other") -> dict:
    """What to do with ONE open ShipStation order: {action, fields, reason}. Pure.
    action: close | skipped_floor | skipped_status | skipped_no_finale |
            skipped_cancelled | skipped_not_shipped."""
    status = str(ss_order.get("orderStatus") or "")
    if status not in OPEN_STATUSES:
        return {"action": "skipped_status", "fields": {}, "reason": f"ShipStation status {status}"}
    created = str(ss_order.get("createDate") or "")[:10]
    if created_after and (not created or created < created_after):
        return {"action": "skipped_floor", "fields": {}, "reason": f"created {created or '?'} before floor {created_after}"}
    if finale_order is None:
        return {"action": "skipped_no_finale", "fields": {}, "reason": "no Finale order -- will retry"}
    if str(finale_order.get("statusId") or "") == "ORDER_CANCELLED":
        return {"action": "skipped_cancelled", "fields": {}, "reason": "Finale order is cancelled -- review in ShipStation"}
    moved = [s for s in shipments if str(s.get("statusId") or "") in MOVED]
    if not moved:
        return {"action": "skipped_not_shipped", "fields": {}, "reason": "not shipped in Finale yet -- will retry"}
    last = sorted(moved, key=lambda s: str(s.get("shipDate") or ""))[-1]
    ship_date = str(last.get("shipDate") or "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {"action": "close",
            "fields": {"carrierCode": carrier_code, "trackingNumber": str(last.get("trackingCode") or ""), "shipDate": ship_date},
            "reason": ""}


def push_shipstation_close(ss_orders: list[dict], *, live: bool = False, only: list[str] | None = None,
                           limit: int | None = None, client=None, finale=None, created_after: str | None = None,
                           carrier_code: str = "other", max_per_run: int | None = None) -> dict:
    """Close (live) or preview (dry) these open ShipStation orders against Finale.
    One row per order: status close_done | would_close | skipped_* | failed.
    Receipts (tracking.shipstation_marks) only for orders actually closed, so a
    waiting order is retried next pass. `max_per_run` counts the orders about to be
    closed; over it the run is refused before the first write."""
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    from app import tracking
    if only is not None:
        wanted = {str(x) for x in only}
        ss_orders = [o for o in ss_orders if str(o.get("orderNumber")) in wanted]
    if limit is not None:
        ss_orders = ss_orders[:limit]
    done = tracking.get_shipstation_marks([str(o.get("orderId")) for o in ss_orders])
    counts = {k: 0 for k in ("close_done", "would_close", "skipped_done", "skipped_floor", "skipped_status", "skipped_no_finale",
                             "skipped_cancelled", "skipped_not_shipped", "failed")}
    results: list[dict] = []
    writers: list[tuple[dict, dict]] = []

    def finish(row, status, **extra):
        row.update({"status": status, **extra}); counts[status] += 1; results.append(row)

    for o in ss_orders:
        oid, po = str(o.get("orderId") or ""), str(o.get("orderNumber") or "")
        row = {"order_id": oid, "po_number": po, "ss_status": o.get("orderStatus"), "created": str(o.get("createDate") or "")[:10],
               "fields": {}, "error": None}
        if oid in done:
            finish(row, "skipped_done", error=f"already closed by this tool ({done[oid].get('updated_at')})"); continue
        try:
            fo = finale.get_order(po) if finale is not None else None
            ships = finale.order_shipments(fo) if (finale is not None and fo) else []
        except Exception as exc:  # noqa: BLE001 -- one bad read must not stop the batch
            finish(row, "failed", error=f"Finale read failed: {exc}"); continue
        p = plan_close(o, fo, ships, created_after=created_after, carrier_code=carrier_code)
        row["fields"], row["error"] = p["fields"], (p["reason"] or None)
        if p["action"] != "close":
            finish(row, p["action"]); continue
        finish(row, "would_close")
        writers.append((row, o))

    blocked = None
    if max_per_run is not None and len(writers) > max_per_run:
        blocked = f"{len(writers)} orders to close exceeds max_per_run {max_per_run} -- nothing written"
    for row, o in (writers if (live and not blocked) else []):
        counts["would_close"] -= 1
        try:
            client.mark_as_shipped(int(o["orderId"]), row["fields"])
            tracking.record_shipstation_mark(row["order_id"], row["po_number"], row["fields"].get("trackingNumber"),
                                             row["fields"].get("shipDate"), "closed")
            row["status"] = "close_done"; counts["close_done"] += 1
        except Exception as exc:  # noqa: BLE001
            row.update(status="failed", error=str(exc)); counts["failed"] += 1
    out = {"mode": "live" if live else "dry", "results": results, "summary": {"candidates": len(ss_orders), **counts}}
    if blocked:
        out["blocked"] = blocked
    return out
