"""The three-process setup (2026-09-21): the web app, the Finale worker and order-watch
run from one repo and share tracking.db. What must hold across processes:
  - ONE Finale run at a time on the box (the flock half of finale_jobs._finale_run);
  - each job id scheduled by ONE process (app.schedule claims);
  - the dashboard sees runs made by the other processes (stored run state, next runs);
  - a worker imports only what its own jobs need.
Cross-process cases use a real second Python process."""
import os
import subprocess
import sys
import textwrap
from unittest.mock import MagicMock, patch

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def client(monkeypatch):
    """The web app with CRSTL mocked, Finale off and no scheduler (conftest)."""
    from fastapi.testclient import TestClient
    monkeypatch.delenv("MOCK_DATA", raising=False)
    monkeypatch.setattr("app.finale.FinaleClient.configured", staticmethod(lambda: False))
    with patch("app.crstl_cache.CrstlClient") as C:
        C.return_value.fetch_invoices.return_value = []
        C.return_value.fetch_po_provinces.return_value = {}
        from app.main import app
        with TestClient(app) as c:
            yield c


def _spawn(code: str, env_db: str) -> subprocess.Popen:
    """Run `code` in another Python process against the same tracking DB; it prints
    READY when its setup is done and then waits for stdin to close."""
    env = {**os.environ, "TRACKING_DB": env_db, "PYTHONPATH": REPO}
    proc = subprocess.Popen([sys.executable, "-c", textwrap.dedent(code)], cwd=REPO, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "READY"
    return proc


def _finish(proc: subprocess.Popen) -> None:
    proc.stdin.close()
    proc.wait(10)


# ── the Finale run lock spans processes ────────────────────────────────────

def test_a_finale_run_in_another_process_makes_this_one_busy(tmp_path, monkeypatch):
    from app import finale_jobs
    db = os.environ["TRACKING_DB"]
    other = _spawn("""
        import sys
        from app import finale_jobs
        with finale_jobs._finale_run("poll"):
            print("READY", flush=True)
            sys.stdin.read()
    """, db)
    try:
        with pytest.raises(finale_jobs.FinaleBusy, match=rf"poll, pid {other.pid}"):
            with finale_jobs._finale_run("edi"):
                pass
        assert finale_jobs.finale_state()["running"] is True       # the dashboard sees it
    finally:
        _finish(other)
    with finale_jobs._finale_run("edi"):                             # released with the process
        pass
    assert finale_jobs.finale_state()["running"] is False


def test_a_dead_holders_note_is_not_a_running_finale_run(tmp_path):
    from app import finale_jobs
    dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
    with open(finale_jobs._run_lock_path(), "w") as f:
        f.write(f"poll pid {dead.pid}")
    assert finale_jobs._run_lock_holder() is None
    assert finale_jobs.finale_state()["running"] is False
    with finale_jobs._finale_run("edi"):
        assert finale_jobs._run_lock_holder() == f"edi, pid {os.getpid()}"
    assert finale_jobs._run_lock_holder() is None                    # the note is cleared on release


# ── the dashboard reads run state other processes stored ───────────────────

def test_run_state_written_by_another_process_reaches_the_dashboard(client):
    db = os.environ["TRACKING_DB"]
    other = _spawn("""
        import sys
        from app import finale_jobs, tracking
        finale_jobs._save_state("edi", {"last_run": "2026-09-21T15:00:00+00:00", "mode": "live",
                                        "summary": {"posted": 2}, "results": [], "blocked": None, "error": None})
        finale_jobs._save_state("dsd", {"last_run": "2026-09-21T15:00:05+00:00", "summary": {"candidates": 3}})
        tracking.set_json("finale_state:alerts", {"last_run": "2026-09-21T15:07:00+00:00", "summary": {"found": 1}})
        print("READY", flush=True)
        sys.stdin.read()
    """, db)
    _finish(other)
    latest = client.get("/api/finale-push/latest").json()
    assert latest["mode"] == "live" and latest["summary"] == {"posted": 2}
    assert latest["dsd"]["summary"] == {"candidates": 3}
    assert latest["alerts"]["summary"] == {"found": 1}
    assert latest["running"] is False


def test_an_edi_error_merges_into_the_stored_run_not_over_it():
    from app import finale_jobs, tracking
    finale_jobs._save_state("edi", {"last_run": "t1", "mode": "live", "summary": {"posted": 1}})
    with patch.dict(finale_jobs._finale_push_state, {"mode": None, "summary": None}):
        finale_jobs._save_state("edi", {"error": "boom"})             # e.g. the ride-along failing
    assert tracking.get_json("finale_state:edi") == {"last_run": "t1", "mode": "live",
                                                     "summary": {"posted": 1}, "error": "boom"}


def test_the_automation_panel_shows_next_runs_stored_by_the_workers(client):
    from app import automation, tracking
    tracking.set_setting("next_run:finale_push", "2026-09-21T15:15:00+00:00")
    tracking.set_setting("next_run:order_alerts", "2026-09-21T15:22:00+00:00")
    sched = MagicMock()
    sched.get_jobs.return_value = []
    with patch("app.main._scheduler", sched):
        jobs = {j["id"]: j for j in client.get("/api/automation").json()["jobs"]}
    assert jobs["finale_push"]["next_run"] == "2026-09-21T15:15:00+00:00"
    assert jobs["finale_dsd"]["next_run"] == "2026-09-21T15:15:00+00:00"     # runs_with finale_push
    assert jobs["order_alerts"]["next_run"] == "2026-09-21T15:22:00+00:00"
    job = MagicMock(id="order_alerts", next_run_time=None)
    automation.record_next_runs(MagicMock(get_jobs=MagicMock(return_value=[job])))
    assert automation.stored_next_run("order_alerts") is None                 # "" = not scheduled


# ── one process per job id ─────────────────────────────────────────────────

def test_web_jobs_default_to_everything_and_reject_a_typo(monkeypatch):
    from app import schedule
    monkeypatch.delenv("SCHEDULER_JOBS", raising=False)
    assert schedule.web_jobs() == schedule.WEB_DEFAULT
    monkeypatch.setenv("SCHEDULER_JOBS", "daily_refresh, netsuite_push,daily_digest")
    assert schedule.web_jobs() == ["daily_refresh", "netsuite_push", "daily_digest"]
    monkeypatch.setenv("SCHEDULER_JOBS", "daily_refresh,finale_psuh")
    with pytest.raises(ValueError, match="finale_psuh"):
        schedule.web_jobs()


def test_a_job_claimed_by_another_process_is_not_scheduled_here(monkeypatch):
    from app import schedule
    db = os.environ["TRACKING_DB"]
    other = _spawn("""
        import sys
        from app import schedule
        held, refused = schedule.claim(["order_alerts"], "worker alerts")
        assert held and not refused
        print("READY", flush=True)
        sys.stdin.read()
    """, db)
    try:
        held, refused = schedule.claim(["daily_digest", "order_alerts"], "crstl-api")
        assert list(held) == ["daily_digest"] and refused == ["order_alerts"]
        assert schedule.claim_holder("order_alerts") == f"worker alerts pid {other.pid}"
        schedule.release(held)
    finally:
        _finish(other)
    held, refused = schedule.claim(["order_alerts"], "crstl-api")          # free once it exits
    assert list(held) == ["order_alerts"] and not refused
    schedule.release(held)


def test_the_web_app_leaves_a_workers_job_to_the_worker(monkeypatch, tmp_path):
    """The web app with the default (every job) still skips one a worker holds, so
    reverting SCHEDULER_JOBS while a worker runs cannot double-schedule it."""
    from app import schedule
    monkeypatch.setenv("TRACKING_DB", str(tmp_path / "tracking.db"))
    monkeypatch.delenv("SCHEDULER_JOBS", raising=False)
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    other = _spawn("""
        import sys
        from app import schedule
        schedule.claim(["finale_push"], "worker finale")
        print("READY", flush=True)
        sys.stdin.read()
    """, os.environ["TRACKING_DB"])
    jobs = {}
    try:
        from fastapi.testclient import TestClient
        with patch("app.crstl_cache.CrstlClient"), patch("app.main.AsyncIOScheduler") as S:
            S.return_value.add_job.side_effect = lambda f, trigger, **kw: jobs.setdefault(kw["id"], trigger)
            S.return_value.get_jobs.return_value = []
            from app.main import app
            with TestClient(app):
                pass
    finally:
        _finish(other)
    assert "finale_push" not in jobs
    assert set(jobs) == set(schedule.WEB_DEFAULT) - {"finale_push"}


def test_production_web_jobs_leave_the_poll_and_alerts_to_the_workers():
    """The crstl-api unit's SCHEDULER_JOBS plus the two workers cover every panel job
    exactly once."""
    from app import schedule
    unit = open(os.path.join(REPO, "deploy/crstl-api.service")).read()
    line = next(l for l in unit.splitlines() if "SCHEDULER_JOBS=" in l)
    web = line.split("SCHEDULER_JOBS=")[1].strip('"').split(",")
    workers = [j for ids in schedule.WORKERS.values() for j in ids]
    everything = web + workers
    assert len(everything) == len(set(everything))
    assert set(schedule.WEB_DEFAULT) <= set(everything)
    for service, unit_name in (("finale", "crstl-finale-worker"), ("alerts", "crstl-order-watch")):
        text = open(os.path.join(REPO, f"deploy/{unit_name}.service")).read()
        assert f"-m app.worker {service}" in text


# ── the workers ────────────────────────────────────────────────────────────

def test_the_alerts_worker_loads_no_finale_or_accounting_code(tmp_path):
    out = subprocess.run([sys.executable, "-c", textwrap.dedent("""
        import sys
        from unittest.mock import MagicMock
        from app import schedule
        schedule.register(MagicMock(), schedule.WORKERS["alerts"])
        print(sorted(m for m in ("app.finale_jobs", "app.accounting", "app.main", "fastapi", "openpyxl")
                     if m in sys.modules))
    """)], cwd=REPO, env={**os.environ, "PYTHONPATH": REPO}, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "['openpyxl']"      # via the CRSTL cache's report helpers; nothing else


def test_the_finale_worker_registers_its_poll_and_its_own_cache_refresh():
    from app import schedule
    sched = MagicMock()
    schedule.register(sched, schedule.WORKERS["finale"])
    ids = {c.kwargs["id"]: c.args[1] for c in sched.add_job.call_args_list}
    assert ids == {"finale_push": "interval", "finale_cache_refresh": "cron"}


def test_a_worker_exits_for_systemd_to_retry_while_another_process_has_its_job():
    from app import schedule, worker
    other = _spawn("""
        import sys
        from app import schedule
        schedule.claim(["order_alerts"], "crstl-api")
        print("READY", flush=True)
        sys.stdin.read()
    """, os.environ["TRACKING_DB"])
    try:
        with patch("app.worker.load_env"):                  # never the real .env in a test
            assert worker.main(["alerts"]) == 1
    finally:
        _finish(other)
    with patch("app.worker.load_env"):
        assert worker.main(["nope"]) == 2
    held, _ = schedule.claim(["order_alerts"], "x")                          # the refusal released it
    assert held
    schedule.release(held)


def test_the_finale_worker_waits_for_a_loaded_cache_before_polling(monkeypatch):
    from app import crstl_cache, worker
    calls = []
    def refresh():
        calls.append(1)
        crstl_cache._cache["status"] = "ok" if len(calls) == 3 else "error: CRSTL down"
    monkeypatch.setattr("app.crstl_cache._refresh_cache", refresh)
    monkeypatch.setattr("app.worker.time.sleep", lambda s: None)
    with patch.dict(crstl_cache._cache, {"status": "never"}):
        worker._load_cache()
    assert len(calls) == 3


def test_the_worker_refresh_logs_only_a_failure(monkeypatch):
    from app import crstl_cache
    with patch("app.crstl_cache._refresh_cache"), patch("app.tracking.record_job_run") as rec, \
         patch.dict(crstl_cache._cache, {"status": "ok"}):
        crstl_cache._run_worker_refresh()
    rec.assert_not_called()
    with patch("app.crstl_cache._refresh_cache"), patch("app.tracking.record_job_run") as rec, \
         patch.dict(crstl_cache._cache, {"status": "error: timeout"}):
        crstl_cache._run_worker_refresh()
    rec.assert_called_once_with("finale_push", "error", "cache refresh: error: timeout")


def test_processes_starting_together_can_all_init_a_fresh_db(tmp_path):
    """The web app and both workers start at once on the box; a first-time init in
    each must not fail with 'database is locked' (seen in a smoke run before the fix)."""
    db = str(tmp_path / "fresh" / "tracking.db")
    env = {**os.environ, "TRACKING_DB": db, "PYTHONPATH": REPO}
    procs = [subprocess.Popen([sys.executable, "-c", "from app import tracking; tracking.init_db()"],
                              cwd=REPO, env=env, stderr=subprocess.PIPE, text=True) for _ in range(4)]
    errors = [p.communicate(timeout=30)[1] for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0, 0], errors
