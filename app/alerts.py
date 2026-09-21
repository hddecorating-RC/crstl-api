"""Order alerts: outliers the digest does not cover, emailed to the order.alerts
group (env ALERT_RECIPIENTS) -- one email per poll, grouped by issue, never the
same order twice, with a "still open" line for earlier ones not yet fixed.
Weekdays only (Toronto), like the digest -- see `alert_day`.

Issue 1 (2026-09-16, two misses in one day after 1 in 173 since August):
    ASN not sent to Home Depot -- a non-voided ShipStation label in the HD Dropship
    store older than `asn_missing_after_minutes` with no 856 in CRSTL for that PO.
    Fix: create the 856 and 810 by hand in CRSTL; the app then invoices as usual.

Adding an issue = one more finder returning rows of the same shape and one more
entry in ISSUES. Ritchie's shape for the email: order # (clickable, opens the PO in
CRSTL), the issue, the known fix -- nothing else.
"""
import html
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/Toronto")
PT = ZoneInfo("America/Los_Angeles")   # ShipStation timestamps carry no zone and are Pacific
CRSTL_PO_URL = "https://omnicrstl.web.app/edi/purchase-order/view/{id}/{id}"

ISSUES = {
    "asn_missing": {
        "title": "ASN not sent to HD",
        "detail": "no ASN {minutes} min after the label",
        "fix": "create the 856 and 810 in CRSTL",
    },
    "packed_unshipped": {
        "title": "Packed, not shipped",
        "detail": "packed over {packed_hours}h ago",
        "fix": "Ship Selected Sales if it went",
    },
}

PACKED = "SHIPMENT_PACKED"


def alert_day(now: datetime | None = None) -> bool:
    """True Mon-Fri in Toronto. The scheduled alerts job skips the whole run on
    Sat/Sun -- no email, no receipts written -- so nothing is lost: an outlier still
    open on Monday has no receipt, is "new" on Monday's first run, and is emailed
    then. One that got fixed over the weekend is never reported."""
    return (now or datetime.now(timezone.utc)).astimezone(ET).weekday() < 5


# An 856 in one of these states was never sent to HD: a Draft is a warehouse
# resubmission in progress, a Rejected one bounced. Neither counts as "ASN sent",
# so neither may silence (or resolve) the alert. Anything else (Send_Success,
# Accepted, or a state CRSTL adds later) counts as sent -- erring towards quiet.
NOT_SENT_STATES = ("Draft", "Rejected")


def sent_asn_pos(asn_states: dict) -> set:
    """PO numbers with an 856 that actually went to HD, from list_transaction_states('856')."""
    return {v["po_number"] for v in asn_states.values() if v.get("state") not in NOT_SENT_STATES}


def crstl_po_url(po_transaction_id: str | None) -> str:
    return CRSTL_PO_URL.format(id=po_transaction_id) if po_transaction_id else ""


def _pt_to_et(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)[:19]).replace(tzinfo=PT).astimezone(ET)
    except ValueError:
        return None


def find_asn_missing(shipments: list[dict], asn_pos: set, po_ids: dict, *, after_minutes: int,
                     now: datetime | None = None) -> list[dict]:
    """Rows for issue 'asn_missing'. `shipments` = ShipStation v1 shipment records
    (HD Dropship store); `asn_pos` = PO numbers that have any 856 in CRSTL;
    `po_ids` = {po_number: 850 transaction id} for the CRSTL link. Pure."""
    now = now or datetime.now(timezone.utc)
    out = []
    for s in shipments:
        if s.get("voided"):
            continue
        po = str(s.get("orderNumber") or "")
        if not po or po in asn_pos:
            continue
        made = _pt_to_et(s.get("createDate") or "")
        if made is None or (now - made) < timedelta(minutes=after_minutes):
            continue
        out.append({"issue": "asn_missing", "key": f"asn_missing:{s.get('shipmentId')}", "po_number": po,
                    "url": crstl_po_url(po_ids.get(po)), "note": f"label {made.strftime('%H:%M ET')}",
                    "tracking": s.get("trackingNumber")})
    return out


def packed_at(shipment: dict) -> datetime | None:
    """When a Finale shipment was packed, from the SHIPMENT_PACKED entry in its status
    history. The shipment LISTING carries that history but no packDate, so this needs
    no extra request."""
    for e in shipment.get("statusIdHistoryList") or []:
        if isinstance(e, dict) and e.get("statusId") == PACKED and e.get("txStamp"):
            try:
                return datetime.fromtimestamp(int(e["txStamp"]), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                return None
    return None


def _packed_sales(shipments: list[dict]):
    """(po, shipment) for every Finale SALE shipment still PACKED, test orders excluded."""
    for s in shipments:
        if str(s.get("statusId") or "") != PACKED or s.get("shipmentTypeId") != "SALES_SHIPMENT":
            continue
        po = str(s.get("primaryOrderUrl") or "").rstrip("/").rsplit("/", 1)[-1]
        if po and not po.upper().startswith("TEST_"):
            yield po, s


def packed_sales_pos(shipments: list[dict]) -> set:
    """The POs of every packed sale shipment -- the ones packed_dropship asks about."""
    return {po for po, _ in _packed_sales(shipments)}


def packed_dropship(shipments: list[dict], dropship_pos: set, *, now: datetime | None = None) -> list[dict]:
    """Every Finale sale shipment still PACKED on a DROPSHIP order, with how long it has
    been packed. The raw material for the alert AND for choosing its threshold. Pure.

    DSD is deliberately excluded (Ritchie, 2026-09-17): a DSD shipment is packed days
    before the pickup its ASN scheduled, so it sits packed by design and any flat
    threshold would report it as late. `dropship_pos` comes from the 850s' own
    trading_partner_flavor, so the split is CRSTL's, not a guess from the PO format.
    """
    now = now or datetime.now(timezone.utc)
    out = []
    for po, s in _packed_sales(shipments):
        if po not in dropship_pos:
            continue
        when = packed_at(s)
        if when is None:
            continue
        out.append({"po_number": po, "shipment_id": s.get("shipmentId"),
                    "shipment_id_user": s.get("shipmentIdUser") or s.get("shipmentId"),
                    "packed_at": when, "hours": (now - when).total_seconds() / 3600})
    return sorted(out, key=lambda r: r["hours"], reverse=True)


def find_packed_unshipped(shipments: list[dict], po_ids: dict, dropship_pos: set, *, after_hours: int,
                          packed_after: str | None = None, now: datetime | None = None) -> list[dict]:
    """Rows for issue 'packed_unshipped': dropship shipments packed more than
    `after_hours` ago and still not shipped. `packed_after` is a positive floor on the
    pack date, so switching this on never alerts the whole history at once."""
    out = []
    for r in packed_dropship(shipments, dropship_pos, now=now):
        if r["hours"] < after_hours:
            continue
        when = r["packed_at"]
        if packed_after and when.astimezone(ET).strftime("%Y-%m-%d") < packed_after:
            continue
        days = int(r["hours"] // 24)
        age = f" ({days}d)" if days >= 2 else ""
        out.append({"issue": "packed_unshipped", "key": f"packed_unshipped:{r['shipment_id']}",
                    "po_number": r["po_number"], "url": crstl_po_url(po_ids.get(r["po_number"])),
                    "note": f"{r['shipment_id_user']}, packed {when.astimezone(ET).strftime('%d %b %H:%M ET')}{age}"})
    return out


def resolved_packed_unshipped(open_receipts: list[dict], packed_keys: set) -> list[dict]:
    """Earlier 'packed_unshipped' receipts whose shipment is no longer packed -- it was
    shipped, or cancelled. Either way the warehouse has dealt with it."""
    return [r for r in open_receipts if r.get("issue") == "packed_unshipped" and r.get("key") not in packed_keys]


def resolved_asn_missing(open_receipts: list[dict], asn_pos: set, voided_keys: set = frozenset()) -> list[dict]:
    """Earlier 'asn_missing' receipts that have cleared: the PO now has a sent 856, or
    the label itself was voided (order cancelled / re-labelled -- no ASN will ever
    come for that label; a replacement label is a new shipment id, alerted afresh)."""
    return [r for r in open_receipts if r.get("issue") == "asn_missing"
            and (str(r.get("po_number")) in asn_pos or r.get("key") in voided_keys)]


def alert_email(new_rows: list[dict], still_open: list[dict], config: dict) -> tuple[str, str]:
    """(subject, html). Subject names the issue (or counts them) and lists up to
    three order numbers; body = per issue: title, detail, fix, linked orders."""
    by_issue: dict[str, list[dict]] = {}
    for r in new_rows:
        by_issue.setdefault(r["issue"], []).append(r)
    orders = [r["po_number"] for r in new_rows]
    head = ", ".join(orders[:3]) + (f" +{len(orders) - 3} more" if len(orders) > 3 else "")
    what = ISSUES[next(iter(by_issue))]["title"] if len(by_issue) == 1 else f"{len(by_issue)} issues"
    subject = f"Order alert: {what} — {head}"

    def link(r):
        po = html.escape(str(r["po_number"]))
        return f'<a href="{html.escape(r["url"])}">{po}</a>' if r.get("url") else po

    h = ""
    for issue, rows in by_issue.items():
        meta = ISSUES[issue]
        detail = meta["detail"].format(minutes=config.get("asn_missing_after_minutes", 60),
                                       packed_hours=config.get("packed_unshipped_after_hours", 24))
        h += (f'<p style="margin:0 0 4px"><strong>{html.escape(meta["title"])}</strong> — '
              f'{html.escape(detail)}. <em>{html.escape(meta["fix"])}.</em></p>'
              f'<ul style="margin:0 0 14px">'
              + "".join(f"<li>{link(r)}{(' — ' + html.escape(r['note'])) if r.get('note') else ''}</li>" for r in rows)
              + "</ul>")
    if still_open:
        h += ('<p style="margin:0">Still open: '
              + ", ".join(f"{link(r)} ({html.escape(str(r.get('sent_et') or ''))})" for r in still_open) + "</p>")
    return subject, h


def run_alerts(shipments: list[dict], asn_pos: set, po_ids: dict, *, config: dict, live: bool,
               recipients: list[str], send, finale_shipments: list[dict] | None = None,
               dropship_pos: set | None = None, now: datetime | None = None) -> dict:
    """Find outliers, diff against receipts, send ONE email for the new ones (live),
    resolve receipts whose issue has cleared. Returns what it found/sent."""
    from app import tracking
    now = now or datetime.now(timezone.utc)
    finale_shipments = finale_shipments or []
    found = find_asn_missing(shipments, asn_pos, po_ids, after_minutes=int(config.get("asn_missing_after_minutes") or 60), now=now)
    found += find_packed_unshipped(finale_shipments, po_ids, dropship_pos or set(),
                                   after_hours=int(config.get("packed_unshipped_after_hours") or 24),
                                   packed_after=(str(config.get("packed_go_live_after") or "") or None), now=now)
    receipts = tracking.get_alert_receipts([r["key"] for r in found])
    new = [r for r in found if r["key"] not in receipts]
    open_all = tracking.open_alert_receipts()
    voided_keys = {f"asn_missing:{s.get('shipmentId')}" for s in shipments if s.get("voided")}
    packed_keys = {f"packed_unshipped:{r['shipment_id']}"
                   for r in packed_dropship(finale_shipments, dropship_pos or set(), now=now)}
    resolved = resolved_asn_missing(open_all, asn_pos, voided_keys)
    resolved += resolved_packed_unshipped(open_all, packed_keys)
    still_open = [{**r, "url": crstl_po_url(po_ids.get(str(r.get("po_number")))),
                   "sent_et": (datetime.fromisoformat(r["sent_at"]).astimezone(ET).strftime("%d %b %H:%M ET") if r.get("sent_at") else "")}
                  for r in open_all if r not in resolved and r["key"] not in {n["key"] for n in new}]
    subject, body = alert_email(new, still_open, config) if new else ("", "")
    sent = False
    if live:
        for r in resolved:
            tracking.resolve_alert(r["key"])
        if new:
            if not recipients:
                raise RuntimeError("ALERT_RECIPIENTS not set")
            send(subject=subject, body_html=body, recipients=recipients)
            for r in new:
                tracking.record_alert(r["key"], r["po_number"], r["issue"])
            sent = True
    return {"mode": "live" if live else "dry", "found": found, "new": new, "still_open": still_open,
            "resolved": [r["key"] for r in resolved], "subject": subject, "body_html": body, "sent": sent,
            "summary": {"found": len(found), "new": len(new), "still_open": len(still_open), "resolved": len(resolved)}}


def alert_recipients() -> list[str]:
    return [r.strip() for r in os.environ.get("ALERT_RECIPIENTS", "").split(",") if r.strip()]
