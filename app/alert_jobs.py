"""Order alerts job: find order outliers (app.alerts) and email the new ones to the
order.alerts group. Its own 15-min job, weekdays only."""
import time
from datetime import datetime, timedelta, timezone

from app import tracking
from app.crstl import created_since
from app.mail import send_mail
from app.shipstation import ShipStationClient
from app.alerts import (after_go_live, alert_day, alert_recipients, find_asn_missing, packed_sales_pos,
                        run_alerts, sent_asn_pos)
from app.edi_rejects import parse_864
from app.netsuite_payload import load_refs
from app.automation import AUTO_ALERTS_SETTING
from app import automation, crstl_cache


def _alerts_config() -> dict:
    return load_refs().get("alerts") or {}


def _run_alerts_job() -> None:
    """Every 15 minutes as its OWN job -- not a pass of the Finale poll, so switching
    Finale invoicing off (or a manual Finale run holding its lock) never silences it:
    a monitor must not depend on what it monitors. Two gates: config alerts.enabled
    and the dashboard toggle. Weekdays only (Toronto), like the digest -- an outlier
    still open on Monday is emailed on Monday's first run."""
    if not _alerts_config().get("enabled"):
        tracking.record_job_run("order_alerts", "skipped", "disabled in config"); return
    if not automation._job_enabled(AUTO_ALERTS_SETTING):
        tracking.record_job_run("order_alerts", "skipped", "disabled"); return
    if not alert_day():
        tracking.record_job_run("order_alerts", "skipped", "weekend -- alerts resume Monday"); return
    try:
        _run_alerts(True)
    except Exception as exc:
        print(f"WARNING: order alerts run failed: {exc}")
        tracking.record_job_run("order_alerts", "error", str(exc)[:200])


# What the alerts need to know about a PO that never changes -- its 850's CRSTL id and
# whether it is dropship -- kept for the life of the process (order-watch runs for days),
# so a non-CRSTL order (HD Supply...) sitting packed is not looked up every 15 minutes.
# A PO CRSTL does not know is asked again after a day.
_PO_850: dict[str, tuple[float, tuple[str, bool] | None]] = {}
_PO_MISS_TTL = 24 * 3600


def _lookup_850(crstl, po: str) -> tuple[str, bool] | None:
    hit = _PO_850.get(po)
    if hit and (hit[1] is not None or time.time() - hit[0] < _PO_MISS_TTL):
        return hit[1]
    rows = crstl.find_850(po)
    meta = (rows[0].get("metadata") or rows[0]) if rows else None
    found = (str(meta.get("id") or rows[0].get("id")),
             "dropship" in str(meta.get("trading_partner_flavor") or "").lower()) if meta else None
    _PO_850[po] = (time.time(), found)
    return found


def _fill_older_pos(crstl, cfg: dict, shipments: list[dict], finale_shipments: list[dict],
                    asn_pos: set, po_ids: dict, dropship_pos: set) -> None:
    """The CRSTL listings cover the last lookback+2 days. A PO the alerts can still ask
    about may be older -- a re-labelled order's earlier ASN, a packed shipment's 850, an
    open alert -- so look those few up exactly (by PO), which gives the same answer as
    listing all history. Updates asn_pos / po_ids / dropship_pos in place."""
    open_all = tracking.open_alert_receipts()
    would_alert = {r["po_number"] for r in find_asn_missing(
        shipments, asn_pos, {}, after_minutes=int(cfg.get("asn_missing_after_minutes") or 60))}
    open_asn = {str(r.get("po_number")) for r in open_all if r.get("issue") == "asn_missing" and r.get("po_number")}
    open_pos = {str(r.get("po_number")) for r in open_all if r.get("po_number")}
    for po in sorted((would_alert | open_pos | packed_sales_pos(finale_shipments)) - set(po_ids)):
        found = _lookup_850(crstl, po)
        if found:
            po_ids[po] = found[0]
            if found[1]:
                dropship_pos.add(po)
    for po in sorted((would_alert | open_asn) - asn_pos):
        if po in po_ids:
            asn_pos.update(sent_asn_pos(crstl.list_transaction_states("856", source_document_ids=po_ids[po])))


# The 850 behind a rejected ASN / invoice, kept for the life of the process: the same
# 864 sits in the window for days and the answer never changes.
_DOC_SOURCE: dict[str, dict | None] = {}


def _lookup_source(crstl, doc: str) -> dict | None:
    """{"po_number", "id"} for the document HD rejected, so the alert links to the PO:
    an ASN number is an 856's reference_id, an invoice number an 810's, and both carry
    their 850 in source_document_id. None when CRSTL has no such document."""
    if doc not in _DOC_SOURCE:
        meta = crstl.find_by_reference("810" if doc.upper().startswith("INV") else "856", doc)
        _DOC_SOURCE[doc] = {"po_number": str(meta.get("source_document_reference_id") or ""),
                            "id": str(meta.get("source_document_id") or "")} if meta else None
    return _DOC_SOURCE[doc]


def _rejections(crstl, cfg: dict) -> tuple[list[dict], dict]:
    """HD's 864 rejections for the alert window, and the source document of each one
    (for the PO link). No go-live date configured = nothing read and nothing reported,
    so switching this on never emails the backlog that predates it; the lookups are
    done only for the 864s that clear that floor."""
    go_live = str(cfg.get("edi_reject_go_live_after") or "")
    if not go_live:
        return [], {}
    rejections = crstl.fetch_rejections(created_after=created_since(int(cfg.get("lookback_days") or 7) + 2))
    sources = {}
    for rec in rejections:
        if not after_go_live(rec.get("created_at"), go_live):
            continue
        parsed = parse_864(rec.get("detail"))
        if parsed["kind"] == "invoice":
            continue                      # the digest reports those, not the warehouse
        for doc in parsed["documents"]:
            found = _lookup_source(crstl, doc)
            if found:
                sources[doc] = found
    return rejections, sources


def _product_count() -> int | None:
    """Finale's product count, read at most once a day (one list request) and kept in
    the settings table -- the catalogue only grows when a product line is onboarded.
    A list that comes back full counts as the limit. None if never read."""
    from app.finale import FinaleClient, FinaleListFull
    stored = tracking.get_json("finale_product_count") or {}
    fresh = stored.get("checked_at") and (datetime.now(timezone.utc) - datetime.fromisoformat(stored["checked_at"])) < timedelta(hours=24)
    if fresh or not FinaleClient.configured():
        return stored.get("count")
    try:
        count = len(FinaleClient()._list("product"))
    except FinaleListFull:
        count = FinaleClient.PAGE_LIMIT
    except Exception as exc:  # noqa: BLE001 -- the alerts go on; the count is retried tomorrow
        print(f"WARNING: order alerts: Finale product count failed: {exc}")
        return stored.get("count")
    tracking.set_json("finale_product_count", {"count": count, "checked_at": datetime.now(timezone.utc).isoformat()})
    return count


def _run_alerts(live: bool) -> dict:
    """Find order outliers (today: ShipStation label with no CRSTL 856) and email the
    new ones to ALERT_RECIPIENTS in ONE message (live), or preview it (dry)."""
    from app.finale import FinaleUnavailable
    if not ShipStationClient.configured():
        raise FinaleUnavailable("SHIPSTATION_V1_KEY / SHIPSTATION_V1_SECRET not set")
    cfg = _alerts_config()
    lookback = int(cfg.get("lookback_days") or 7)
    since = (datetime.now(timezone.utc) - timedelta(days=lookback)).strftime("%Y-%m-%d")
    shipments = ShipStationClient().list_shipments(int(cfg.get("dropship_store_id") or 0), since)
    crstl = crstl_cache._get_client()
    window = created_since(lookback + 2)        # recent CRSTL only; _fill_older_pos covers the rest
    asn_pos = sent_asn_pos(crstl.list_transaction_states("856", created_after=window))  # Draft/Rejected do not count as sent
    orders_850 = [(tx.get("metadata") or tx) for tx in crstl._fetch_all_transactions("850", created_after=window)]
    po_ids = {str(m.get("reference_id")): str(m.get("id")) for m in orders_850}
    # The dropship/DSD split comes from CRSTL's own flavour on the 850, not the PO format.
    dropship_pos = {str(m.get("reference_id")) for m in orders_850
                    if "dropship" in str(m.get("trading_partner_flavor") or "").lower()}
    from app.finale import FinaleClient
    finale_shipments = FinaleClient().list_shipments() if FinaleClient.configured() else []
    _fill_older_pos(crstl, cfg, shipments, finale_shipments, asn_pos, po_ids, dropship_pos)
    rejections, doc_sources = _rejections(crstl, cfg)
    result = run_alerts(shipments, asn_pos, po_ids, config=cfg, live=live, recipients=alert_recipients(),
                        send=send_mail, finale_shipments=finale_shipments, dropship_pos=dropship_pos,
                        product_count=_product_count(), product_limit=FinaleClient.PAGE_LIMIT,
                        rejections=rejections, doc_sources=doc_sources)
    # The dashboard's "last alerts run" (read back by finale_jobs.finale_state()).
    tracking.set_json("finale_state:alerts", {"last_run": datetime.now(timezone.utc).isoformat(),
                                              **{k: v for k, v in result.items() if k != "body_html"}})
    c = result["summary"]
    tracking.record_job_run("order_alerts", "ok",
                            f"{c['found']} outlier(s): {c['new']} new{' (emailed)' if result['sent'] else ''}, "
                            f"{c['still_open']} still open, {c['resolved']} resolved [{'live' if live else 'dry'}]")
    return result
