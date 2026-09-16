"""Order alerts: outliers the digest does not cover, emailed to the order.alerts
group (env ALERT_RECIPIENTS) -- one email per poll, grouped by issue, never the
same order twice, with a "still open" line for earlier ones not yet fixed.

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
        "title": "ASN not sent to Home Depot",
        "detail": "label created in ShipStation, no ASN in CRSTL after {minutes} min.",
        "fix": "create the 856 and 810 in CRSTL.",
    },
}


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


def resolved_asn_missing(open_receipts: list[dict], asn_pos: set) -> list[dict]:
    """Earlier 'asn_missing' receipts whose PO now has an 856 -- to mark resolved."""
    return [r for r in open_receipts if r.get("issue") == "asn_missing" and str(r.get("po_number")) in asn_pos]


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
        h += (f'<p><strong>{html.escape(meta["title"])}</strong> — '
              f'{html.escape(meta["detail"].format(minutes=config.get("asn_missing_after_minutes", 60)))}<br>'
              f'Fix: {html.escape(meta["fix"])}</p><ul>'
              + "".join(f"<li>{link(r)}{(' — ' + html.escape(r['note'])) if r.get('note') else ''}</li>" for r in rows)
              + "</ul>")
    if still_open:
        h += ("<p>Still open: " + ", ".join(f"{link(r)} (alerted {html.escape(str(r.get('sent_et') or ''))})" for r in still_open) + "</p>")
    return subject, h


def run_alerts(shipments: list[dict], asn_pos: set, po_ids: dict, *, config: dict, live: bool,
               recipients: list[str], send, now: datetime | None = None) -> dict:
    """Find outliers, diff against receipts, send ONE email for the new ones (live),
    resolve receipts whose issue has cleared. Returns what it found/sent."""
    from app import tracking
    now = now or datetime.now(timezone.utc)
    found = find_asn_missing(shipments, asn_pos, po_ids, after_minutes=int(config.get("asn_missing_after_minutes") or 60), now=now)
    receipts = tracking.get_alert_receipts([r["key"] for r in found])
    new = [r for r in found if r["key"] not in receipts]
    open_all = tracking.open_alert_receipts()
    resolved = resolved_asn_missing(open_all, asn_pos)
    still_open = [{**r, "url": crstl_po_url(po_ids.get(str(r.get("po_number")))),
                   "sent_et": (datetime.fromisoformat(r["sent_at"]).astimezone(ET).strftime("%m-%d %H:%M ET") if r.get("sent_at") else "")}
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
