"""Order alerts job: find order outliers (app.alerts) and email the new ones to the
order.alerts group. Its own 15-min job, weekdays only."""
from datetime import datetime, timedelta, timezone

from app import tracking
from app.mail import send_mail
from app.shipstation import ShipStationClient
from app.alerts import alert_day, alert_recipients, run_alerts, sent_asn_pos
from app.netsuite_payload import load_refs
from app.automation import AUTO_ALERTS_SETTING
from app.finale_jobs import _finale_push_lock, _finale_push_state
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


def _run_alerts(live: bool) -> dict:
    """Find order outliers (today: ShipStation label with no CRSTL 856) and email the
    new ones to ALERT_RECIPIENTS in ONE message (live), or preview it (dry)."""
    from app.finale import FinaleUnavailable
    if not ShipStationClient.configured():
        raise FinaleUnavailable("SHIPSTATION_V1_KEY / SHIPSTATION_V1_SECRET not set")
    cfg = _alerts_config()
    since = (datetime.now(timezone.utc) - timedelta(days=int(cfg.get("lookback_days") or 7))).strftime("%Y-%m-%d")
    shipments = ShipStationClient().list_shipments(int(cfg.get("dropship_store_id") or 0), since)
    crstl = crstl_cache._get_client()
    asn_pos = sent_asn_pos(crstl.list_transaction_states("856"))       # Draft/Rejected do not count as sent
    orders_850 = [(tx.get("metadata") or tx) for tx in crstl._fetch_all_transactions("850")]
    po_ids = {str(m.get("reference_id")): str(m.get("id")) for m in orders_850}
    # The dropship/DSD split comes from CRSTL's own flavour on the 850, not the PO format.
    dropship_pos = {str(m.get("reference_id")) for m in orders_850
                    if "dropship" in str(m.get("trading_partner_flavor") or "").lower()}
    from app.finale import FinaleClient
    finale_shipments = FinaleClient().list_shipments() if FinaleClient.configured() else []
    result = run_alerts(shipments, asn_pos, po_ids, config=cfg, live=live, recipients=alert_recipients(),
                        send=send_mail, finale_shipments=finale_shipments, dropship_pos=dropship_pos)
    with _finale_push_lock:
        _finale_push_state["alerts"] = {"last_run": datetime.now(timezone.utc).isoformat(),
                                        **{k: v for k, v in result.items() if k != "body_html"}}
    c = result["summary"]
    tracking.record_job_run("order_alerts", "ok",
                            f"{c['found']} outlier(s): {c['new']} new{' (emailed)' if result['sent'] else ''}, "
                            f"{c['still_open']} still open, {c['resolved']} resolved [{'live' if live else 'dry'}]")
    return result
