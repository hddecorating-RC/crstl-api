"""A weekday with nothing new still gets the 5 AM digest (Ritchie, 2026-11-17: on 11-17 nothing shipped, no digest
went out, and accounting thought it was broken). The email says plainly that there are no new sales orders."""
from unittest.mock import patch

from app import accounting, tracking

EMPTY = {"new_sos": [], "gaps": [], "so_map": {}, "finale": {}, "recon": None, "nonedi": None}


def test_a_quiet_weekday_still_sends_the_digest():
    with patch("app.accounting._auto_digest_enabled", return_value=True), \
         patch("app.accounting._last_digest_sent_at", return_value=None), \
         patch("app.accounting._so_digest_data", return_value=EMPTY), \
         patch("app.accounting._send_digest_safe") as send:
        accounting._digest_after_scheduled_push("2026-11-17T10:00:00+00:00", None)
    send.assert_called_once()
    assert not any(r["detail"] == "nothing to report" for r in tracking.recent_job_runs(job="daily_digest"))


def test_the_empty_digest_says_no_new_sales_orders_without_a_table():
    body = accounting._so_digest_html(EMPTY, [], quiet=True)
    assert "No new sales orders" in body
    assert "<table" not in body


def test_a_failed_push_never_says_no_new_invoices():
    with patch("app.accounting._so_digest_data", return_value=EMPTY), \
         patch("app.accounting.send_mail") as send, \
         patch.dict("os.environ", {"MAIL_RECIPIENTS": "acct@example.com"}):
        accounting._send_daily_digest(push_problem="It couldn't reach NetSuite")
    body = send.call_args.kwargs["body_html"]
    assert "No new sales orders" not in body
    assert "0 sales order(s)" in body
