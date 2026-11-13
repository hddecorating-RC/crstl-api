"""The accounting digest when Microsoft 365 is down (2026-11-13: Graph 502 at 05:02 and 05:04, the 05:00 push had
already written 33 SOs to NetSuite; Ritchie sent the digest by hand at 09:00 but the dashboard kept the 05:05 error).

1. A manual send does the same bookkeeping as a scheduled one: job run "ok [manual]", error cleared, last-sent moved.
2. digest_retry: weekdays, every hour 06:00-17:00 ET, resends ONLY when today's latest digest attempt failed and
   nothing has gone out since. Email only: it never touches NetSuite."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import accounting, tracking

RESULT = {"sent_to": ["acct@example.com"], "count": 33, "gaps": 0, "to_watch": 0, "subject": "s", "mode": "so_digest"}


def _client():
    from app.main import app
    return TestClient(app)


def _state(**kw):
    with accounting._digest_lock:
        accounting._digest_state.update(kw)


# ── 1. manual send ─────────────────────────────────────────────────────────

def test_manual_send_records_ok_clears_the_error_and_moves_last_sent():
    _state(error="Graph sendMail failed (502)", last_sent=None, sending=False)
    with patch("app.accounting._send_daily_digest", return_value=RESULT):
        resp = _client().post("/api/email/send-digest")
    assert resp.status_code == 200
    assert accounting._digest_state["error"] is None
    assert accounting._digest_state["last_sent"]
    run = tracking.recent_job_runs(job="daily_digest")[0]
    assert run["status"] == "ok" and "[manual]" in run["detail"] and "33 SO(s)" in run["detail"]
    assert accounting._last_digest_sent_at() == run["ran_at"]       # the next digest's "since" starts here


def test_manual_send_failure_is_recorded_and_reported():
    _state(error=None, sending=False)
    with patch("app.accounting._send_daily_digest", side_effect=RuntimeError("Graph sendMail failed (502)")):
        resp = _client().post("/api/email/send-digest")
    assert resp.status_code == 500
    run = tracking.recent_job_runs(job="daily_digest")[0]
    assert run["status"] == "error" and "[manual]" in run["detail"]
    assert "502" in accounting._digest_state["error"]


def test_manual_send_refused_while_another_send_is_running():
    _state(sending=True)
    try:
        with patch("app.accounting._send_daily_digest") as send:
            resp = _client().post("/api/email/send-digest")
        assert resp.status_code == 409
        send.assert_not_called()
    finally:
        _state(sending=False)


# ── 2. digest_retry ────────────────────────────────────────────────────────

NOW = datetime(2026, 11, 13, 11, 0, tzinfo=timezone.utc)         # 06:00 ET, a Friday


def _run(status, detail="", at=None):
    tracking.record_job_run("daily_digest", status, detail)
    if at is not None:   # backdate the row just written
        import contextlib
        with contextlib.closing(tracking._connect()) as conn, conn:
            conn.execute("UPDATE job_runs SET ran_at = ? WHERE id = (SELECT MAX(id) FROM job_runs)", (at.isoformat(),))


def test_retry_due_after_a_failure_this_morning():
    _run("ok", "2 SO(s) [post-push]", NOW - timedelta(days=1))
    _run("error", "Graph sendMail failed (502) [after push]", NOW - timedelta(minutes=55))
    assert accounting._digest_retry_due(NOW) is True


def test_no_retry_once_anything_has_gone_out_since():
    _run("error", "Graph sendMail failed (502) [after push]", NOW - timedelta(minutes=55))
    _run("ok", "33 SO(s), 0 issue(s) [manual]", NOW - timedelta(minutes=5))
    assert accounting._digest_retry_due(NOW) is False


def test_no_retry_for_yesterdays_failure_or_a_quiet_day():
    _run("error", "Graph sendMail failed (502) [after push]", NOW - timedelta(days=1))
    assert accounting._digest_retry_due(NOW) is False             # tomorrow's 05:00 digest carries it
    assert accounting._digest_retry_due(NOW + timedelta(days=3)) is False


def test_no_retry_when_the_last_run_was_skipped_or_nothing_to_report():
    _run("error", "Graph sendMail failed (502) [after push]", NOW - timedelta(hours=2))
    _run("ok", "nothing to report", NOW - timedelta(minutes=30))
    assert accounting._digest_retry_due(NOW) is False


def test_retry_job_sends_only_when_due_and_enabled(monkeypatch):
    monkeypatch.setattr("app.accounting._auto_digest_enabled", lambda: True)
    with patch("app.accounting._digest_retry_due", return_value=True), \
         patch("app.accounting._send_digest_safe") as send:
        accounting._run_digest_retry_job()
    send.assert_called_once_with("retry")
    with patch("app.accounting._digest_retry_due", return_value=False), \
         patch("app.accounting._send_digest_safe") as send:
        accounting._run_digest_retry_job()
    send.assert_not_called()
    monkeypatch.setattr("app.accounting._auto_digest_enabled", lambda: False)
    with patch("app.accounting._digest_retry_due", return_value=True), \
         patch("app.accounting._send_digest_safe") as send:
        accounting._run_digest_retry_job()
    send.assert_not_called()


def test_retry_job_never_touches_netsuite(monkeypatch):
    monkeypatch.setattr("app.accounting._auto_digest_enabled", lambda: True)
    with patch("app.accounting._digest_retry_due", return_value=True), \
         patch("app.accounting._send_digest_safe"), \
         patch("app.accounting.push_invoices") as push:
        accounting._run_digest_retry_job()
    push.assert_not_called()


def test_retry_is_scheduled_weekdays_every_hour_6_to_17_et():
    from app import schedule
    target, trigger, opts = schedule.JOBS["digest_retry"]
    assert target == "app.accounting:_run_digest_retry_job" and trigger == "cron"
    assert (opts["day_of_week"], opts["hour"], opts["minute"], opts["timezone"]) == ("mon-fri", "6-17", 0, "America/Toronto")
    assert "digest_retry" in schedule.WEB_DEFAULT
