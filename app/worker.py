"""Run one service's scheduled jobs in their own process.

    python -m app.worker finale   # crstl-finale-worker: the 15-min Finale poll
    python -m app.worker alerts   # crstl-order-watch:   order alerts

No web server: the dashboard stays in the web app (crstl-api), which reads what these
record in tracking.db (job_runs, last-run state, next-run times). See app.schedule.

Exits non-zero, for systemd to retry, if another process already schedules one of
its jobs -- e.g. the web app before its SCHEDULER_JOBS was narrowed. On SIGTERM it
waits for a running job to finish, like the web app's shutdown does, so a restart
never cuts a Finale write in half.
"""
import signal
import sys
import threading
import time

from app.env import load_env

USAGE = "usage: python -m app.worker {finale|alerts}"


def _load_cache() -> None:
    """The Finale worker must not poll on an empty CRSTL cache: it is what tells an
    EDI order from a non-EDI one. Retry the full refresh until it lands."""
    from app import crstl_cache
    delay = 30
    while True:
        crstl_cache._refresh_cache()
        if crstl_cache._cache_loaded():
            print(f"finale worker: CRSTL cache loaded ({len(crstl_cache._cache['invoices'])} invoices)")
            return
        print(f"finale worker: CRSTL cache not loaded ({crstl_cache._cache.get('status')}) -- retrying in {delay}s")
        time.sleep(delay)
        delay = min(delay * 2, 600)


def main(argv: list[str]) -> int:
    load_env()
    from apscheduler.schedulers.background import BackgroundScheduler
    from app import automation, schedule, tracking

    if len(argv) != 1 or argv[0] not in schedule.WORKERS:
        print(USAGE, file=sys.stderr)
        return 2
    service = argv[0]
    job_ids = schedule.WORKERS[service]
    tracking.init_db()
    held, refused = schedule.claim(job_ids, f"worker {service}")
    if refused:
        holders = {j: schedule.claim_holder(j) for j in refused}
        print(f"{service} worker: already scheduled elsewhere {holders} -- exiting; systemd retries")
        schedule.release(held)
        return 1
    if service == "finale":
        _load_cache()

    scheduler = BackgroundScheduler()
    schedule.register(scheduler, job_ids)
    scheduler.start()
    automation.track_next_runs(scheduler)
    print(f"{service} worker: running {', '.join(job_ids)}")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.wait(1):
        pass
    print(f"{service} worker: stopping -- waiting for a running job to finish")
    scheduler.shutdown(wait=True)
    schedule.release(held)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
