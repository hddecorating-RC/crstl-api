"""Automation control shared by every service: the on/off toggles (settings table),
the job registry that drives the dashboard's Automation panel, and _job_enabled."""
from app import tracking


AUTO_DIGEST_SETTING = "auto_digest_enabled"

# ---------------------------------------------------------------- Finale invoicing
# ONE tool invoices BOTH channels in Finale (internal record; goes nowhere
# downstream). It rides the NetSuite push event: the invoices that just landed as
# SOs get their Finale invoice in the same run, so both systems are created from
# one 810-Accepted moment. Two gates, BOTH must be on: config finale.enabled (ships
# OFF) and this runtime toggle (dashboard; default OFF). Manual: POST /api/finale.
AUTO_FINALE_SETTING = "auto_finale_enabled"
# DSD pickup numbers (PRO/RTS from the Accepted 856 onto the Finale shipment). Runs
# inside the Finale invoicing poll, so it is ALSO off whenever that job is off.
AUTO_DSD_SETTING = "auto_dsd_prefill_enabled"


# ---- Automation control: on/off toggles, schedule, and run logs ----------
# Each scheduled job self-checks its toggle (persisted in the settings table)
# and records a run in job_runs, so the dashboard can show state + history.
AUTO_SYNC_SETTING = "auto_sync_enabled"
AUTO_NS_EXPORT_SETTING = "auto_ns_export_enabled"
AUTO_NS_PUSH_SETTING = "auto_ns_push_enabled"
# Order alerts were already live when they got their own job (2026-09-21), so the
# toggle defaults ON -- the deploy changes when they run, not whether they run.
AUTO_ALERTS_SETTING = "auto_alerts_enabled"

# Registry drives the /api/automation panel. `default` "false" means the job is
# off until someone turns it on (the NetSuite auto-push stays off until
# accounting signs off). Keep `id` in sync with the scheduler job ids below.
AUTOMATION_JOBS = [
    {"id": "daily_refresh", "label": "Invoice sync (Crstl)", "schedule": "Daily · 4:45 AM ET",   "setting": AUTO_SYNC_SETTING,    "default": "true"},
    {"id": "netsuite_push", "label": "NetSuite auto-push",   "schedule": "Mon–Fri · 5:00 AM ET", "setting": AUTO_NS_PUSH_SETTING, "default": "false"},
    {"id": "daily_digest",  "label": "Daily digest email",   "schedule": "Mon–Fri · 7:15 AM ET", "setting": AUTO_DIGEST_SETTING,  "default": "true"},
    {"id": "finale_push",   "label": "Finale invoicing",     "schedule": "Mon–Fri · every 15 min · 6:00 AM–6:45 PM ET", "setting": AUTO_FINALE_SETTING,  "default": "false"},
    # Not its own scheduler job: it is the third pass of finale_push (runs_with),
    # so its next run is that job's, and it is silent whenever that job is off.
    {"id": "finale_dsd",    "label": "DSD pickup numbers → Finale (PRO / RTS)", "schedule": "Mon–Fri · 6:00 AM–6:45 PM ET · inside Finale invoicing",
     "setting": AUTO_DSD_SETTING, "default": "false", "runs_with": "finale_push"},
    {"id": "order_alerts",  "label": "Order alerts email",   "schedule": "Mon–Fri · every 15 min · 7:07 AM–5:52 PM ET", "setting": AUTO_ALERTS_SETTING, "default": "true"},
]
_JOB_BY_ID = {j["id"]: j for j in AUTOMATION_JOBS}


def _job_enabled(setting: str, default: str = "true") -> bool:
    return (tracking.get_setting(setting, default) or default).lower() != "false"


# Next-run times. The Automation panel reads them from the web app's own scheduler,
# but the Finale poll and order alerts are scheduled by the workers, so every process
# stores its jobs' next run in the settings table after each run.
def record_next_runs(scheduler) -> None:
    for job in scheduler.get_jobs():
        nrt = getattr(job, "next_run_time", None)
        tracking.set_setting(f"next_run:{job.id}", nrt.isoformat() if nrt else "")


def track_next_runs(scheduler) -> None:
    """Record now (call after scheduler.start()) and again after every run."""
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
    scheduler.add_listener(lambda _event: record_next_runs(scheduler),
                           EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED)
    record_next_runs(scheduler)


def stored_next_run(job_id: str) -> str | None:
    return tracking.get_setting(f"next_run:{job_id}") or None
