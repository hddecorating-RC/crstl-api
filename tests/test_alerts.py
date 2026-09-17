"""Order alerts (app.alerts): a ShipStation label with no CRSTL 856 after N minutes
is emailed ONCE to the order.alerts group, one email per run grouped by issue, order
numbers in the subject and clickable in the body, still-open ones listed, resolved
when the 856 appears."""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.alerts import alert_email, crstl_po_url, find_asn_missing, run_alerts

NOW = datetime(2026, 9, 16, 21, 0, tzinfo=timezone.utc)                 # 17:00 ET
CFG = {"asn_missing_after_minutes": 60}
def lab(po, sid, create_pt, voided=False, trk="5207559"):               # ShipStation timestamps are Pacific
    return {"shipmentId": sid, "orderNumber": po, "createDate": create_pt, "voided": voided, "trackingNumber": trk}
IDS = {"538871711": "6aaad4ce5f4cb0423a06a6c5", "538873990": "abc", "538858722": "def"}


def test_finder_applies_age_voided_and_existing_asn():
    ships = [lab("538871711", 1, "2026-09-16T12:45:20"),      # 15:45 ET, 75 min old -> outlier
             lab("538873990", 2, "2026-09-16T13:30:00"),      # 16:30 ET, 30 min old -> too young
             lab("538869151", 3, "2026-09-16T10:02:20"),      # has an 856
             lab("538800001", 4, "2026-09-16T09:00:00", voided=True)]
    out = find_asn_missing(ships, {"538869151"}, IDS, after_minutes=60, now=NOW)
    assert [r["po_number"] for r in out] == ["538871711"]
    r = out[0]
    assert r == {"issue": "asn_missing", "key": "asn_missing:1", "po_number": "538871711",
                 "url": "https://omnicrstl.web.app/edi/purchase-order/view/6aaad4ce5f4cb0423a06a6c5/6aaad4ce5f4cb0423a06a6c5",
                 "note": "label 15:45 ET", "tracking": "5207559"}
    assert crstl_po_url(None) == ""


def test_email_shape_subject_orders_and_grouping():
    rows = [{"issue": "asn_missing", "key": "k1", "po_number": "538871711", "url": crstl_po_url("x1"), "note": "label 15:45 ET"},
            {"issue": "asn_missing", "key": "k2", "po_number": "538873990", "url": "", "note": "label 16:10 ET"}]
    subject, body = alert_email(rows, [], CFG)
    assert subject == "Order alert: ASN not sent to Home Depot — 538871711, 538873990"
    assert '<a href="https://omnicrstl.web.app/edi/purchase-order/view/x1/x1">538871711</a>' in body
    assert "<li>538873990 — label 16:10 ET</li>" in body                      # no id -> plain text, still listed
    assert "Fix: create the 856 and 810 in CRSTL." in body and "after 60 min" in body
    assert "Still open" not in body
    many = [{**rows[0], "po_number": str(i)} for i in range(5)]
    assert alert_email(many, [], CFG)[0].endswith("— 0, 1, 2 +2 more")
    s2, b2 = alert_email(rows[:1], [{"po_number": "538858722", "url": crstl_po_url("def"), "sent_et": "09-16 11:40 ET"}], CFG)
    assert "Still open: <a href=" in b2 and "538858722</a> (alerted 09-16 11:40 ET)" in b2


def test_run_alerts_sends_once_lists_still_open_and_resolves(tmp_path, monkeypatch):
    from app import tracking
    monkeypatch.setenv("TRACKING_DB", str(tmp_path / "t.db")); tracking.init_db()
    ships = [lab("538871711", 1, "2026-09-16T12:45:20"), lab("538858722", 9, "2026-09-16T07:34:59")]
    send = MagicMock()
    # dry: found but nothing sent, nothing receipted
    d = run_alerts(ships, set(), IDS, config=CFG, live=False, recipients=["order.alerts@x"], send=send, now=NOW)
    assert d["summary"] == {"found": 2, "new": 2, "still_open": 0, "resolved": 0} and not d["sent"] and send.call_count == 0
    assert d["subject"] == "Order alert: ASN not sent to Home Depot — 538871711, 538858722"
    # live: ONE email for both, two receipts
    l1 = run_alerts(ships, set(), IDS, config=CFG, live=True, recipients=["order.alerts@x"], send=send, now=NOW)
    assert l1["sent"] and send.call_count == 1
    assert send.call_args.kwargs["recipients"] == ["order.alerts@x"] and "538871711" in send.call_args.kwargs["subject"]
    # next run, nothing new: no email; both listed as still open
    l2 = run_alerts(ships, set(), IDS, config=CFG, live=True, recipients=["order.alerts@x"], send=send, now=NOW)
    assert not l2["sent"] and send.call_count == 1 and {r["po_number"] for r in l2["still_open"]} == {"538871711", "538858722"}
    # 538858722 gets its 856 by hand: resolved, drops off; a NEW miss goes out with the other still open
    ships3 = ships + [lab("538873990", 2, "2026-09-16T12:50:00")]
    l3 = run_alerts(ships3, {"538858722"}, IDS, config=CFG, live=True, recipients=["order.alerts@x"], send=send, now=NOW)
    assert l3["resolved"] == ["asn_missing:9"] and l3["sent"] and send.call_count == 2
    assert [r["po_number"] for r in l3["new"]] == ["538873990"] and [r["po_number"] for r in l3["still_open"]] == ["538871711"]
    assert "Still open: " in send.call_args.kwargs["body_html"] and "538871711" in send.call_args.kwargs["body_html"]
    assert tracking.open_alert_receipts() and all(r["po_number"] != "538858722" for r in tracking.open_alert_receipts())
    # no recipients configured: live refuses rather than sending nowhere
    with pytest.raises(RuntimeError):
        run_alerts(ships3 + [lab("538879999", 5, "2026-09-16T12:00:00")], {"538858722"}, IDS, config=CFG, live=True, recipients=[], send=send, now=NOW)


def test_only_sent_asns_silence_or_resolve_the_alert():
    """A Draft (warehouse resubmission in progress) or Rejected 856 is not an ASN HD
    has: the PO stays an outlier, and an earlier alert for it is not resolved."""
    from app.alerts import resolved_asn_missing, sent_asn_pos
    states = {"a": {"po_number": "1", "state": "Accepted"}, "b": {"po_number": "2", "state": "Send_Success"},
              "c": {"po_number": "3", "state": "Draft"}, "d": {"po_number": "4", "state": "Rejected"},
              "e": {"po_number": "5", "state": "Draft"}, "f": {"po_number": "5", "state": "Accepted"}}   # resubmitted then accepted
    assert sent_asn_pos(states) == {"1", "2", "5"}
    ships = [lab("3", 3, "2026-09-16T12:00:00"), lab("4", 4, "2026-09-16T12:00:00"), lab("1", 1, "2026-09-16T12:00:00")]
    assert [r["po_number"] for r in find_asn_missing(ships, sent_asn_pos(states), {}, after_minutes=60, now=NOW)] == ["3", "4"]
    assert resolved_asn_missing([{"issue": "asn_missing", "po_number": "3", "key": "k"}], sent_asn_pos(states)) == []
