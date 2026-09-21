"""Every scheduled job, defined once, and which process runs it.

Three processes on the box since 2026-09-21 (all from this repo, one systemd unit
each -- see DEPLOY.md):

    crstl-api            the web app: dashboard + every manual endpoint, and the
                         jobs in SCHEDULER_JOBS (production: the invoice sync, the
                         NetSuite push and the accounting digest)
    crstl-finale-worker  python -m app.worker finale  -- the 15-min Finale poll
    crstl-order-watch    python -m app.worker alerts  -- the order alerts job

With SCHEDULER_JOBS unset the web app runs every job itself, as it did before the
split, so a dev box or a rollback needs no other unit.

A job id is scheduled by at most ONE process at a time: each process claims its jobs
with an flock (held for its lifetime, dropped by the kernel if it dies) and never
schedules a job another process holds. Two schedulers firing the same job is how the
digest once went out twice (07:15 and 07:19).
"""
import fcntl
import importlib
import os
from datetime import datetime, timedelta, timezone

from app import tracking

TORONTO = "America/Toronto"

# id -> ("module:function", trigger, trigger options).
# misfire_grace_time lets a job run late if the host was paused or the scheduler was
# down at fire time (LXC snapshots, restarts); without it a missed 4:45 refresh
# silently vanishes until the next day. Jobs run through toggle-aware wrappers
# (_run_*_job) that self-check their on/off setting and record each run in job_runs,
# so the Automation panel can show state + history. A disabled job still fires but
# no-ops and logs "skipped".
JOBS: dict[str, tuple[str, str, dict]] = {
    "daily_refresh": ("app.crstl_cache:_run_refresh_job", "cron",
                      dict(hour=4, minute=45, timezone=TORONTO, misfire_grace_time=3600, coalesce=True)),
    # NetSuite auto-push -- 5:00 AM ET, before anyone in accounting is entering
    # invoices, so our writes never collide with a manual entry. Runs after the 4:45
    # refresh (fresh data) and before the 7:15 digest (which reports it). OFF by
    # default until accounting turns it on.
    "netsuite_push": ("app.accounting:_run_netsuite_push_job", "cron",
                      dict(day_of_week="mon-fri", hour=5, minute=0, timezone=TORONTO,
                           misfire_grace_time=3600, coalesce=True)),
    # Weekdays only -- nobody works the digest queue on Sat/Sun, so a weekend send is
    # just two emails to ignore. Skipping them loses nothing: the digest sends
    # whatever tracking.db still has unemailed, so Monday 07:15 carries Friday's late
    # invoices plus anything Crstl added over the weekend.
    "daily_digest": ("app.accounting:_run_daily_digest_job", "cron",
                     dict(day_of_week="mon-fri", hour=7, minute=15, timezone=TORONTO,
                          misfire_grace_time=3600, coalesce=True)),
    # Finale invoicing poll -- every 15 minutes. HD accepts the 810 a median 6 min
    # after the ship, so this lands the Finale invoice + order completion ~15-20 min
    # after shipping with exact 810 cents. Cheap when idle (one CRSTL list call). OFF
    # unless config finale.enabled AND the dashboard toggle are both on.
    "finale_push": ("app.finale_jobs:_run_finale_push_job", "interval",
                    dict(minutes=15, misfire_grace_time=600, coalesce=True)),
    # Order alerts -- every 15 minutes, its own job (see alert_jobs._run_alerts_job).
    # Offset 7 minutes from the Finale poll so the two don't hit CRSTL and Finale at once.
    "order_alerts": ("app.alert_jobs:_run_alerts_job", "interval",
                     dict(minutes=15, misfire_grace_time=600, coalesce=True, offset_minutes=7)),
    # The Finale worker's own 4:45 refresh: its CRSTL cache is its own, not the web
    # app's. Not an Automation-panel job (daily_refresh is the one shown).
    "finale_cache_refresh": ("app.crstl_cache:_run_worker_refresh", "cron",
                             dict(hour=4, minute=45, timezone=TORONTO, misfire_grace_time=3600, coalesce=True)),
}

# What the web app runs when SCHEDULER_JOBS is unset: everything, as before the split.
WEB_DEFAULT = ["daily_refresh", "netsuite_push", "daily_digest", "finale_push", "order_alerts"]
# python -m app.worker <service> -> the jobs that service runs.
WORKERS = {"finale": ["finale_push", "finale_cache_refresh"], "alerts": ["order_alerts"]}


def web_jobs() -> list[str]:
    """The web app's jobs: SCHEDULER_JOBS (comma-separated ids) or WEB_DEFAULT. An
    unknown id is an error -- a typo must not silently drop a job."""
    raw = os.environ.get("SCHEDULER_JOBS", "").strip()
    ids = [j.strip() for j in raw.split(",") if j.strip()] if raw else list(WEB_DEFAULT)
    unknown = [j for j in ids if j not in JOBS]
    if unknown:
        raise ValueError(f"SCHEDULER_JOBS names unknown job(s) {unknown}; known: {sorted(JOBS)}")
    return ids


def _claim_path(job_id: str) -> str:
    folder = os.path.dirname(os.path.abspath(tracking._db_path()))
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"scheduler-{job_id}.lock")


def claim_holder(job_id: str) -> str | None:
    """The note left by the process holding this job's claim (may be stale if it died)."""
    try:
        with open(_claim_path(job_id)) as f:
            return f.read().strip() or None
    except OSError:
        return None


def claim(job_ids: list[str], owner: str) -> tuple[dict[str, int], list[str]]:
    """Take the claim on each job id this process will schedule. Returns ({id: fd}
    held, [ids another process holds]). Held until release() or process exit."""
    held, refused = {}, []
    for jid in job_ids:
        fd = os.open(_claim_path(jid), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            refused.append(jid)
            continue
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{owner} pid {os.getpid()}".encode(), 0)
        held[jid] = fd
    return held, refused


def release(held: dict[str, int]) -> None:
    for fd in held.values():
        try:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    held.clear()


def register(scheduler, job_ids, now: datetime | None = None) -> None:
    """Add these jobs to the scheduler, resolving each function only now -- a worker
    imports just the modules its own jobs need."""
    now = now or datetime.now(timezone.utc)
    for jid in job_ids:
        target, trigger, options = JOBS[jid]
        options = dict(options)
        offset = options.pop("offset_minutes", None)
        if offset:
            options["next_run_time"] = now + timedelta(minutes=offset)
        module, name = target.split(":")
        scheduler.add_job(getattr(importlib.import_module(module), name), trigger, id=jid, **options)
