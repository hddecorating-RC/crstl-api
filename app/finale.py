"""Actual ship dates from Finale, for the DSD rows the 856 cannot fill.

A DSD ASN carries a scheduled pickup date and no ship date -- app/shipments.py
has the evidence -- so the workbook's DSD Ship Date comes from here instead.
Dropship and Wholesale keep reading the ASN: that date is what we transmitted
to HD, and the report's job is to show what was sent.

Three things about this API, all measured rather than assumed:

  * Lists come back COLUMN-major -- {field: [v1, v2]}, not [{field: v1}, ...].

  * The ship date is on the SHIPMENT, not the order. Order-level `shipDate` is
    populated on ~17 of 338 orders and cannot be relied on; shipment-level is
    populated on 326 of 326 records that have moved.

  * `shipDate` is a date at a fixed offset, not a moment: the clock reads
    16:00:00 or 19:00:00 and never anything else. Take the date part as it
    stands. Calibrated against 56 Dropship ASNs -- whose ASN date is the
    known-real ship date -- the date part matched exactly on 49 and was one
    day later on 7, so shifting it a day would move the bulk the wrong way.

The join is `primaryOrderUrl`, whose last segment is the Finale order id, and
that id IS the HD PO number -- the same key the workbook already uses for
Province, Product and the ASN dates.

A DSD ship date arrives AFTER the invoice. HD raises the invoice at pickup
time and the goods move days later, so a same-day export legitimately has no
DSD ship date to show; the cell fills on a later re-export. That is why a
missing date here is left blank rather than treated as an error.
"""
import os

import requests

# Both mean the goods have left. Filtering to SHIPMENT_SHIPPED alone drops the
# 122 records that have since been marked delivered.
MOVED = ("SHIPMENT_SHIPPED", "SHIPMENT_DELIVERED")

ENV_KEYS = ("FINALE_ACCOUNT_ID", "FINALE_API_KEY", "FINALE_API_SECRET")


class FinaleUnavailable(RuntimeError):
    """Finale cannot be reached or is not configured. The report continues
    without it: a blank Ship Date is a gap, a wrong one is an error."""


def to_rows(response):
    """Finale's column-major lists, transposed into ordinary rows."""
    if isinstance(response, list):
        return response
    keys = list(response.keys())
    if not keys:
        return []
    first = response[keys[0]]
    if not isinstance(first, list):
        return [response]
    return [{k: response[k][i] for k in keys} for i in range(len(first))]


def shipped_date_of(shipment) -> str:
    """The date part of a shipment's shipDate, clock time discarded."""
    return str(shipment.get("shipDate") or "")[:10]


def ship_date_index(shipments) -> dict:
    """{po_number: ship date} over Finale shipment records.

    A PO with several shipments reports its LAST movement: the order is not
    fully shipped until the last one leaves, and an earlier date would
    overstate how long ago it went. 7 POs on record carry more than one.
    """
    by_po = {}
    for shipment in shipments:
        if shipment.get("statusId") not in MOVED:
            continue
        po = str(shipment.get("primaryOrderUrl") or "").rstrip("/").rsplit("/", 1)[-1]
        date = shipped_date_of(shipment)
        if not po or not date:
            continue
        by_po[po] = max(by_po.get(po, ""), date)
    return by_po


class FinaleClient:
    """Read-only client for the one thing the workbook needs from Finale."""

    TIMEOUT = 90
    PAGE_LIMIT = 10000
    # API-returned URLs are host-relative paths ("/{account}/api/order/123");
    # prefix this to follow them. Never construct a product URL by hand -- resolve
    # through the catalogue (product_index): productId != productUrl slug on a
    # third of the catalogue.
    HOST = "https://app.finaleinventory.com"

    def __init__(self, account_id=None, api_key=None, api_secret=None):
        account_id = account_id if account_id is not None else os.environ.get("FINALE_ACCOUNT_ID", "")
        api_key = api_key if api_key is not None else os.environ.get("FINALE_API_KEY", "")
        api_secret = api_secret if api_secret is not None else os.environ.get("FINALE_API_SECRET", "")
        if not (account_id and api_key and api_secret):
            raise FinaleUnavailable(
                "Finale needs FINALE_ACCOUNT_ID, FINALE_API_KEY and FINALE_API_SECRET"
            )
        self.account_id = account_id
        self.base_url = f"{self.HOST}/{account_id}/api"
        self.session = requests.Session()
        self.session.auth = (api_key, api_secret)

    @staticmethod
    def configured() -> bool:
        """Whether the report can ask Finale at all. Checked before building a
        client so a deployment without Finale keys degrades to blank DSD ship
        dates instead of failing the export."""
        return all(os.environ.get(k) for k in ENV_KEYS)

    def fetch_ship_dates(self) -> dict:
        """{po_number: ship date} for everything Finale has shipped.

        Fetched in one request because `offset` does not work on this endpoint
        -- asking for offset=50 returns the same first row as offset=0 -- so
        there is no way to page. The limit is set far above the current volume
        (446 shipments on record) and a full page is reported rather than
        silently truncated, because the failure mode otherwise is ship dates
        quietly going missing for the oldest orders.
        """
        resp = self.session.get(f"{self.base_url}/shipment",
                                params={"limit": self.PAGE_LIMIT}, timeout=self.TIMEOUT)
        resp.raise_for_status()
        rows = to_rows(resp.json())
        if len(rows) >= self.PAGE_LIMIT:
            print(f"WARNING: Finale returned {len(rows)} shipments, the maximum asked "
                  f"for — some ship dates may be missing. Raise FinaleClient.PAGE_LIMIT.")
        return ship_date_index(rows)

    # ------------------------------------------------------------------ writes
    # Finale invoicing (app.finale_invoice). The legacy /api supports these as
    # undocumented POSTs -- proven 2026-09-15 by DevTools capture + an API-key
    # write test on TEST_0005: POST /api/invoice/ creates (always creates, even
    # with invoiceUrl set), the server stores invoiceItemList exactly as sent,
    # and POST {invoiceUrl}/complete posts it. Every write rewrites Finale's
    # audit stamp to the API login; only call these from the guarded push path.

    def _get(self, path_or_url: str, **params) -> dict:
        url = path_or_url if path_or_url.startswith("http") else self.HOST + path_or_url
        resp = self.session.get(url, params=params or None, timeout=self.TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def product_index(self) -> dict:
        """{productId or UPC: productUrl} over the whole catalogue, in one listing.

        productId is HD's item number (the 850's stock_keeping_unit); the UPC is
        the fallback key. The value is the productUrl path exactly as the API
        returned it, so an invoice line references the product the way Finale
        addresses it. First occurrence wins on a duplicate key.
        """
        rows = to_rows(self._get(f"{self.base_url}/product", limit=self.PAGE_LIMIT))
        if len(rows) >= self.PAGE_LIMIT:
            print(f"WARNING: Finale returned {len(rows)} products, the maximum asked for "
                  f"-- the product index may be incomplete. Raise FinaleClient.PAGE_LIMIT.")
        index: dict = {}
        for p in rows:
            url = str(p.get("productUrl") or "")
            if not url:
                continue
            for key in (str(p.get("productId") or "").strip(),
                        str(p.get("universalProductCode") or "").strip()):
                if key:
                    index.setdefault(key, url)
        return index

    def get_order(self, order_id: str) -> dict | None:
        """The Finale sale order whose id is this HD PO number, or None if there is
        no such order (404). Read before creating an invoice so a missing order is a
        clean skip rather than an invoice pointing at nothing."""
        resp = self.session.get(f"{self.base_url}/order/{order_id}", timeout=self.TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def order_invoices(self, order: dict) -> list[dict]:
        """Every invoice already on this order (followed via invoiceUrlList)."""
        return [self._get(u) for u in (order.get("invoiceUrlList") or [])]

    def shipment_qty_for_order(self, order: dict) -> dict | None:
        """{productUrl: quantity} actually SHIPPED on this order (moved shipments
        only), for the qty cross-check against the 810. None when the order has no
        moved shipment yet -- 'unverified', not 'mismatch'. Item rows are read
        defensively (productUrl, else productId; quantity else 0) so an unexpected
        shape degrades to unverified rather than a false mismatch."""
        seen = False
        qty: dict = {}
        for u in (order.get("shipmentUrlList") or []):
            sh = self._get(u)
            if sh.get("statusId") not in MOVED:
                continue
            seen = True
            for it in (sh.get("shipmentItemList") or []):
                if not isinstance(it, dict):
                    continue
                key = str(it.get("productUrl") or it.get("productId") or "")
                if not key:
                    continue
                try:
                    qty[key] = qty.get(key, 0.0) + float(it.get("quantity") or 0)
                except (TypeError, ValueError):
                    pass
        return qty if seen else None

    def order_shipments(self, order: dict) -> list[dict]:
        """Every shipment on this order (followed via shipmentUrlList), full records."""
        return [self._get(u) for u in (order.get("shipmentUrlList") or [])]

    def update_shipment(self, shipment_url: str, fields: dict) -> dict:
        """POST {shipmentUrl} with a PARTIAL body -- Finale merges the fields given and
        leaves the rest (status, pack location, items) untouched. Proven 2026-09-15 on
        TEST_0005-3: trackingCode + publicNotes written on an INPUT and on a PACKED
        shipment, status unchanged, and both survived the warehouse's manual Ship click.
        Never send shipDateEstimated: the Ship dialog adopts it as the real ship date."""
        body = {"shipmentUrl": shipment_url, **fields}
        resp = self.session.post(self.HOST + shipment_url, json=body, timeout=self.TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def create_invoice(self, body: dict) -> dict:
        """POST /api/invoice/ -- creates a DRAFT (INVOICE_IN_PROCESS) from exactly the
        invoiceItemList given. Returns the created invoice (invoiceId, invoiceUrl,
        invoiceIdUser, actionUrlComplete...)."""
        resp = self.session.post(f"{self.base_url}/invoice/", json=body, timeout=self.TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def complete_invoice(self, invoice_url: str) -> dict:
        """POST {invoiceUrl}/complete -- posts the draft (INVOICE_APPROVED). Locked
        after this; only call once the invoice reconciles."""
        resp = self.session.post(self.HOST + invoice_url.rstrip("/") + "/complete", json={},
                                 timeout=self.TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def complete_order(self, order: dict) -> dict | None:
        """POST the order's own actionUrlComplete (as the API returned it) -- moves it
        to ORDER_COMPLETED so it leaves the actionable sales-order list. None when the
        order exposes no complete action (already completed/cancelled). Only called
        after its invoice is POSTED: completion locks the order against new invoices
        and edits (proven 2026-09-15)."""
        url = order.get("actionUrlComplete")
        if not url:
            return None
        resp = self.session.post(self.HOST + url, json={}, timeout=self.TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------ non-EDI reads
    def list_sale_orders(self) -> list[dict]:
        """Every sale order in one listing (orderId, statusId, saleSourceId, orderDate,
        invoiceUrlList, shipmentUrlList, orderRoleList...). The non-EDI invoicer
        classifies from this list and GETs an order individually only to build."""
        rows = to_rows(self._get(f"{self.base_url}/order", limit=self.PAGE_LIMIT))
        return [r for r in rows if r.get("orderTypeId") == "SALES_ORDER"]

    def party_province_index(self) -> dict:
        """{partyId: province} from GET /api/partygroup -- the customer's POSTAL_ADDRESS
        stateProvinceGeoId (HD Supply Canada 100022 -> "ON"). Customers without a
        postal address (e.g. the EDI 'Home Depot Canada - Dropship' party) are simply
        absent, so the caller skips them rather than guessing a tax province."""
        index: dict = {}
        for p in to_rows(self._get(f"{self.base_url}/partygroup", limit=self.PAGE_LIMIT)):
            pid = str(p.get("partyId") or "").strip() or str(p.get("partyUrl") or "").rstrip("/").rsplit("/", 1)[-1]
            for cm in (p.get("contactMechList") or []):
                if isinstance(cm, dict) and cm.get("contactMechTypeId") == "POSTAL_ADDRESS" and cm.get("stateProvinceGeoId"):
                    index[pid] = str(cm["stateProvinceGeoId"]).upper()
                    break
        return index

    def reopen_order(self, order: dict) -> dict:
        """Reopen a completed order: POST its actionUrlEdit (-> ORDER_CREATED, editable),
        then actionUrlLock (-> ORDER_LOCKED, committed) so it is back exactly where it
        was before completion. Proven 2026-09-15 on 538852414. Used for orders the
        ShipStation connection completed on the ship event with no shipment/invoice --
        Ritchie's rule: that is not "complete"; it gets re-completed properly."""
        edit = order.get("actionUrlEdit")
        if not edit:
            raise RuntimeError(f"order {order.get('orderId')} exposes no edit action; cannot reopen")
        resp = self.session.post(self.HOST + edit, json={}, timeout=self.TIMEOUT)
        resp.raise_for_status()
        reopened = self.get_order(str(order.get("orderId"))) or {}
        if reopened.get("statusId") == "ORDER_CREATED" and reopened.get("actionUrlLock"):
            resp = self.session.post(self.HOST + reopened["actionUrlLock"], json={}, timeout=self.TIMEOUT)
            resp.raise_for_status()
            reopened = self.get_order(str(order.get("orderId"))) or reopened
        return reopened
