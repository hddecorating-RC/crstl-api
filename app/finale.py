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

    def __init__(self, account_id=None, api_key=None, api_secret=None):
        account_id = account_id if account_id is not None else os.environ.get("FINALE_ACCOUNT_ID", "")
        api_key = api_key if api_key is not None else os.environ.get("FINALE_API_KEY", "")
        api_secret = api_secret if api_secret is not None else os.environ.get("FINALE_API_SECRET", "")
        if not (account_id and api_key and api_secret):
            raise FinaleUnavailable(
                "Finale needs FINALE_ACCOUNT_ID, FINALE_API_KEY and FINALE_API_SECRET"
            )
        self.base_url = f"https://app.finaleinventory.com/{account_id}/api"
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
