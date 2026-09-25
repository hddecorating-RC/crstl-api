import asyncio
import contextlib
import os
import pathlib
from datetime import date
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import BaseModel, Field

from app import tracking
from app.mail import MailConfigError
from app.netsuite_payload import load_refs
from app.report import XLSX_MEDIA_TYPE
from app.accounting import (
    ReportUnavailable, _digest_lock, _digest_state, _netsuite_lock, _netsuite_push_lock,
    _netsuite_push_state, _netsuite_state)
from app.automation import AUTOMATION_JOBS, AUTO_DIGEST_SETTING, _JOB_BY_ID
from app.crstl_cache import _cache, _cache_lock
from app.finale_jobs import FinaleBusy, _finale_push_lock, _finale_push_state
from app import accounting, alert_jobs, automation, crstl_cache, env, finale_jobs, schedule


env.load_env()
_scheduler = None  # AsyncIOScheduler, set in lifespan; used to read next-run times


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    tracking.init_db()
    await asyncio.to_thread(crstl_cache._refresh_cache)

    # Set SCHEDULER_ENABLED=false on a dev workstation so a local `uvicorn --reload`
    # can't fire the daily digest / NetSuite export / Crstl refresh in parallel
    # with the production LXC. Running two schedulers against the same Crstl
    # tenant produced two digest emails at 07:15 and 07:19 with different
    # counts because each instance has its own tracking.db. Defaults to enabled
    # so the LXC just works after `systemctl restart`.
    if os.environ.get("SCHEDULER_ENABLED", "true").lower() in ("0", "false", "no"):
        print("Scheduler disabled via SCHEDULER_ENABLED — daily jobs will not run in this instance.")
        yield
        return

    # Which jobs this process runs: SCHEDULER_JOBS, or every job (the pre-split setup)
    # when it is unset. A job another process already holds (the Finale worker,
    # order-watch) is left to it -- see app.schedule.
    global _scheduler
    held, refused = schedule.claim(schedule.web_jobs(), "crstl-api")
    for jid in refused:
        print(f"WARNING: {jid} is scheduled by another process ({schedule.claim_holder(jid)}) -- not here")
    _scheduler = AsyncIOScheduler()
    schedule.register(_scheduler, list(held))
    _scheduler.start()
    automation.track_next_runs(_scheduler)
    print(f"Scheduler: running {', '.join(held) or 'no jobs'}")
    yield
    _scheduler.shutdown()
    schedule.release(held)


app = FastAPI(title="HD Decorating Invoice Dashboard", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict:
    """Lightweight liveness probe for container orchestrators. Does not touch
    the cache lock or the Crstl API. Includes tracking-DB write health so
    persistence failures surface before they produce duplicate digest emails."""
    return {"status": "ok", "tracking": tracking.write_health()}


@app.get("/api/invoices")
def get_invoices() -> dict:
    with _cache_lock:
        invoices = list(_cache["invoices"])
        last_synced = _cache["last_synced"]
        status = _cache["status"]

    if invoices:
        tx_ids = [inv["transaction_id"] for inv in invoices]
        events = tracking.get_latest_events(tx_ids)
        invoices = [
            {**inv, **events.get(inv["transaction_id"], {"exported_at": None, "netsuite_at": None}),
             "netsuite_customer": accounting._netsuite_customer(inv)}
            for inv in invoices
        ]

    # The go-live cutoff (automation config) so the dashboard can hide the
    # pre-cutoff backlog by default -- those older invoices are handled and only
    # clutter the "not pushed" view.
    go_live_after = (load_refs().get("automation") or {}).get("go_live_after") or ""
    return {"invoices": invoices, "last_synced": last_synced, "status": status,
            "go_live_after": go_live_after}


@app.post("/api/sync")
def sync() -> dict:
    crstl_cache._refresh_cache()
    with _cache_lock:
        snapshot = {**_cache}
    return {"ok": snapshot["status"] == "ok", "last_synced": snapshot["last_synced"], "status": snapshot["status"]}


class ExportRequest(BaseModel):
    ids: Optional[list[str]] = None


@app.post("/api/export")
def export(body: ExportRequest = ExportRequest()) -> Response:
    with _cache_lock:
        invoices = list(_cache["invoices"])
    if not invoices:
        return JSONResponse(
            status_code=503,
            content={"message": "Cache is empty. Trigger /api/sync first."},
        )
    # A bulk export is a report, so it carries Accepted only. An explicit id
    # list is a deliberate pick from the dashboard and is honoured as given.
    if body.ids is None:
        invoices = accounting._reportable(invoices)
    else:
        wanted = set(body.ids)
        invoices = [inv for inv in invoices if inv["transaction_id"] in wanted]
    if not invoices:
        return JSONResponse(
            status_code=404,
            content={"message": "No invoices matched the request."},
        )

    try:
        xlsx_bytes = accounting._workbook_for(invoices)
    except ReportUnavailable as exc:
        return JSONResponse(status_code=502, content={"message": str(exc)})

    tracking.record_events([inv["transaction_id"] for inv in invoices], "exported")

    filename = f"invoices_{date.today().isoformat()}.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class NetsuitePushRequest(BaseModel):
    # Dry run by default: a live send must be asked for explicitly. `ids` pushes
    # just those transaction_ids (the "test one invoice" flow); `limit` caps the
    # batch. There is no sandbox, so the safe path is dry_run -> ids -> live.
    # limit must be >= 1 when given: `limit=0` used to fall through to "no cap".
    dry_run: bool = True
    ids: Optional[list[str]] = None
    limit: Optional[int] = Field(default=None, ge=1)
    # Off by default: a record already in NetSuite is skipped, never overwritten.
    # Set true to explicitly UPDATE existing records (the "confirm to update" flow).
    confirm_existing: bool = False


@app.post("/api/netsuite")
async def netsuite_push(body: NetsuitePushRequest = NetsuitePushRequest()) -> JSONResponse:
    """Manual push of Crstl invoices into NetSuite as invoices (TBA REST).
    Dry run unless dry_run=false. A live send is refused (nothing written) while
    any item/tax id is unresolved in config -- the unresolved list comes back so
    it can be filled first."""
    # A live send over this HTTP surface (the app has no auth of its own) MUST be
    # scoped to named invoices. Blocks the "one unauthenticated POST pushes every
    # invoice live to production" path; the operator CLI on the box can still do a
    # deliberate full-batch live send.
    if not body.dry_run and not body.ids:
        return JSONResponse(status_code=400, content={
            "message": "A live push must name the invoices to send (ids). "
                       "Use dry_run for an unscoped preview."})
    with _netsuite_push_lock:
        if _netsuite_push_state.get("running"):
            return JSONResponse(status_code=409, content={"message": "A NetSuite push is already in progress"})
        _netsuite_push_state["running"] = True
    try:
        result = await asyncio.to_thread(accounting._run_netsuite_push, not body.dry_run, body.ids, body.limit,
                                         body.confirm_existing)
    except Exception as exc:
        print(f"NetSuite push failed: {exc}")   # detail to journald, not to the caller
        with _netsuite_push_lock:
            _netsuite_push_state["error"] = "push failed — see server logs"
        return JSONResponse(status_code=500, content={"message": "NetSuite push failed — see server logs"})
    finally:
        with _netsuite_push_lock:
            _netsuite_push_state["running"] = False
    with _netsuite_push_lock:
        last_run = _netsuite_push_state["last_run"]
    # A live send blocked on unresolved ids wrote nothing -> surface as 400.
    status_code = 400 if result.get("blocked") else 200
    return JSONResponse(status_code=status_code, content={**result, "last_run": last_run})


class FinalePushRequest(BaseModel):
    dry_run: bool = True
    ids: Optional[list[str]] = None
    limit: Optional[int] = Field(default=None, ge=1)


@app.post("/api/finale")
async def finale_push(body: FinalePushRequest = FinalePushRequest()) -> JSONResponse:
    """Manual Finale invoicing. Dry run unless dry_run=false; a live run must name
    the invoices (ids) -- same guard as /api/netsuite -- and is refused while any
    promo/tax-rate id is unresolved in config."""
    if not body.dry_run and not body.ids:
        return JSONResponse(status_code=400, content={
            "message": "A live Finale run must name the invoices to create (ids). Use dry_run for a preview."})
    try:
        result = await asyncio.to_thread(finale_jobs._run_finale_push, not body.dry_run, body.ids, body.limit)
    except FinaleBusy as exc:
        return JSONResponse(status_code=409, content={"message": str(exc)})
    except Exception as exc:
        print(f"Finale push failed: {exc}")
        finale_jobs._save_state("edi", {"error": "push failed — see server logs"})
        return JSONResponse(status_code=500, content={"message": "Finale push failed — see server logs"})
    with _finale_push_lock:
        last_run = _finale_push_state["last_run"]
    return JSONResponse(status_code=400 if result.get("blocked") else 200, content={**result, "last_run": last_run})


@app.post("/api/finale/nonedi")
async def finale_nonedi_push(body: FinalePushRequest = FinalePushRequest()) -> JSONResponse:
    """Manual non-EDI Finale invoicing (HD Supply, Special Orders, ...). Dry run unless
    dry_run=false; a live run must name the Finale order ids."""
    if not body.dry_run and not body.ids:
        return JSONResponse(status_code=400, content={
            "message": "A live non-EDI run must name the Finale order ids (ids). Use dry_run for a preview."})
    try:
        result = await asyncio.to_thread(finale_jobs._run_nonedi_push, not body.dry_run, body.ids, body.limit)
    except FinaleBusy as exc:
        return JSONResponse(status_code=409, content={"message": str(exc)})
    except Exception as exc:
        print(f"non-EDI Finale push failed: {exc}")
        return JSONResponse(status_code=500, content={"message": "non-EDI Finale push failed — see server logs"})
    return JSONResponse(status_code=400 if result.get("blocked") else 200, content=result)


@app.post("/api/finale/dsd")
async def finale_dsd_prefill(body: FinalePushRequest = FinalePushRequest()) -> JSONResponse:
    """Manual DSD pre-fill (PRO/RTS from Accepted 856s onto open Finale shipments). Dry
    run unless dry_run=false; a live run must name the ASN ids."""
    if not body.dry_run and not body.ids:
        return JSONResponse(status_code=400, content={
            "message": "A live DSD run must name the ASN ids (ids). Use dry_run for a preview."})
    try:
        result = await asyncio.to_thread(finale_jobs._run_dsd_prefill, not body.dry_run, body.ids, body.limit)
    except FinaleBusy as exc:
        return JSONResponse(status_code=409, content={"message": str(exc)})
    except Exception as exc:
        print(f"DSD prefill failed: {exc}")
        return JSONResponse(status_code=500, content={"message": "DSD prefill failed — see server logs"})
    return JSONResponse(status_code=400 if result.get("blocked") else 200, content=result)


@app.post("/api/shipstation/close")
async def shipstation_close(body: FinalePushRequest = FinalePushRequest()) -> JSONResponse:
    """Mark DSD ShipStation orders shipped once Finale has shipped them. Dry by
    default; a live run must name order numbers (ids) like every other writer."""
    if not body.dry_run and not body.ids:
        return JSONResponse(status_code=400, content={"message": "live run requires ids (ShipStation order numbers)"})
    try:
        result = finale_jobs._run_shipstation_close(not body.dry_run, body.ids, body.limit)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"message": str(exc)[:200]})
    return JSONResponse(content=result)


@app.post("/api/dropship/prefill")
async def dropship_prefill(body: FinalePushRequest = FinalePushRequest()) -> JSONResponse:
    """Write carrier + tracking onto packed dropship shipments. Dry by default; a
    live run must name PO numbers, like every other writer."""
    if not body.dry_run and not body.ids:
        return JSONResponse(status_code=400, content={"message": "live run requires ids (PO numbers)"})
    try:
        result = finale_jobs._run_dropship_prefill(not body.dry_run, body.ids, body.limit)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"message": str(exc)[:200]})
    return JSONResponse(content=result)


@app.post("/api/alerts/check")
async def alerts_check(body: FinalePushRequest = FinalePushRequest()) -> JSONResponse:
    """Find order outliers; dry (default) previews the email, live sends it."""
    try:
        result = alert_jobs._run_alerts(not body.dry_run)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"message": str(exc)[:200]})
    return JSONResponse(content=result)


@app.get("/api/finale-push/latest")
async def finale_push_latest() -> JSONResponse:
    return JSONResponse(content=finale_jobs.finale_state())


@app.get("/api/netsuite-push/latest")
def netsuite_push_latest() -> dict:
    with _netsuite_push_lock:
        return {**_netsuite_push_state}


@app.get("/api/netsuite-export/latest")
def netsuite_export_latest() -> dict:
    with _netsuite_lock:
        state = {**_netsuite_state}
    path = state.get("path")
    state["available"] = bool(path and pathlib.Path(path).exists())
    return state


@app.get("/api/netsuite-export/download")
def netsuite_export_download() -> Response:
    with _netsuite_lock:
        path = _netsuite_state.get("path")
    if not path or not pathlib.Path(path).exists():
        return JSONResponse(
            status_code=404,
            content={"message": "No NetSuite export file available. Generate one first."},
        )
    filename = pathlib.Path(path).name
    return Response(
        content=pathlib.Path(path).read_bytes(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/netsuite-export/generate")
async def netsuite_export_generate() -> dict:
    with _netsuite_lock:
        if _netsuite_state.get("generating"):
            return JSONResponse(status_code=409, content={"message": "Export already in progress"})
        _netsuite_state["generating"] = True
    try:
        await asyncio.to_thread(accounting._generate_netsuite_export)
    except Exception as exc:
        return JSONResponse(status_code=500, content={"message": str(exc)})
    finally:
        with _netsuite_lock:
            _netsuite_state["generating"] = False
    with _netsuite_lock:
        state = {**_netsuite_state}
    path = state.get("path")
    state["available"] = bool(path and pathlib.Path(path).exists())
    return state


class EmailRequest(BaseModel):
    ids: Optional[list[str]] = None


@app.post("/api/email/send-digest")
async def send_digest_now(body: EmailRequest = EmailRequest()) -> JSONResponse:
    """Send an email of invoices. Without `ids`: unemailed digest (same as
    scheduled job). With `ids`: send exactly those invoices."""
    if body.ids is not None and not body.ids:
        return JSONResponse(status_code=400, content={"message": "ids is empty"})
    try:
        result = await asyncio.to_thread(accounting._send_daily_digest, body.ids)
    except MailConfigError as exc:
        return JSONResponse(status_code=400, content={"message": str(exc)})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"message": str(exc)})
    return JSONResponse(result)


@app.post("/api/email/mark-all-emailed")
def mark_all_emailed() -> dict:
    """Baseline-reset the digest: mark every currently-cached invoice as
    already emailed WITHOUT sending anything. Use after a dev/prod tracking
    DB split or when you want to reset the "unemailed" state. Tomorrow's
    scheduled digest will only pick up invoices Crstl adds after this call."""
    with _cache_lock:
        tx_ids = [inv["transaction_id"] for inv in _cache["invoices"] if inv.get("transaction_id")]
    if not tx_ids:
        return {"marked": 0, "message": "cache is empty"}
    # Only mark ones not already marked, so we don't inflate the event log
    unemailed = tracking.get_unemailed_ids(tx_ids)
    tracking.record_events(unemailed, "emailed")
    return {"marked": len(unemailed), "already_emailed": len(tx_ids) - len(unemailed), "total_cached": len(tx_ids)}


@app.get("/api/email/status")
def email_status() -> dict:
    with _digest_lock:
        state = {**_digest_state}
    # Fall back to the tracking DB after a restart wipes in-memory state.
    # Only surfaces sends that actually marked invoices — a 0-count heartbeat
    # right before restart won't be recoverable.
    if state.get("last_sent") is None:
        state["last_sent"] = tracking.latest_event_time("emailed")
    state["auto_enabled"] = accounting._auto_digest_enabled()
    return state


class AutoDigestToggle(BaseModel):
    enabled: bool


@app.get("/api/finale/reconciliation.xlsx")
async def finale_reconciliation_download():
    """The Finale reconciliation sheet that used to ride on accounting's email."""
    content = await asyncio.to_thread(accounting._finale_workbook)
    if content is None:
        return JSONResponse({"detail": "Finale reconciliation is off"}, status_code=404)
    return Response(content=content, media_type=XLSX_MEDIA_TYPE,
                    headers={"Content-Disposition": 'attachment; filename="finale_reconciliation.xlsx"'})


@app.post("/api/email/auto-digest")
def set_auto_digest(body: AutoDigestToggle) -> dict:
    """Enable or disable the weekday digest sent after the 5:00 push (Toronto).
    Persisted in tracking.db so the setting survives restarts. Manual sends are
    always available, including on weekends."""
    tracking.set_setting(AUTO_DIGEST_SETTING, "true" if body.enabled else "false")
    return {"auto_enabled": body.enabled}


@app.get("/api/invoice-checks")
def get_invoice_checks() -> dict:
    """Read-only preview of the digest's 'invoices to watch in HD's portal' section
    (app.invoice_checks): what the next digest would show, whether or not the section
    is enabled. Writes nothing -- the record is only kept once a digest is sent."""
    chk = accounting._invoice_check_data()
    chk.pop("current", None)
    return {"enabled": bool(accounting._invoice_checks_config().get("enabled")), **chk}


@app.get("/api/automation")
def automation_status() -> dict:
    """The scheduled jobs with their on/off state, schedule, next run, and last
    run — drives the Automation panel."""
    next_runs = {}
    if _scheduler is not None:
        for j in _scheduler.get_jobs():
            nrt = getattr(j, "next_run_time", None)
            next_runs[j.id] = nrt.isoformat() if nrt else None
    last = {}
    for run in tracking.recent_job_runs(300):
        last.setdefault(run["job"], run)  # first seen = most recent (DESC order)
    jobs = [{
        "id": j["id"], "label": j["label"], "schedule": j["schedule"],
        "enabled": automation._job_enabled(j["setting"], j["default"]),
        # This process's own scheduler, else what the process that runs it stored.
        "next_run": (next_runs.get(j.get("runs_with") or j["id"])
                     or automation.stored_next_run(j.get("runs_with") or j["id"])),
        "last_run": last.get(j["id"]),
    } for j in AUTOMATION_JOBS]
    return {"jobs": jobs, "scheduler_running": _scheduler is not None}


class AutomationToggle(BaseModel):
    job: str
    enabled: bool


@app.post("/api/automation")
def automation_toggle(body: AutomationToggle) -> JSONResponse:
    """Turn a scheduled job on or off. Persisted in tracking.db (survives
    restarts). A disabled job still fires on schedule but no-ops and logs it."""
    job = _JOB_BY_ID.get(body.job)
    if not job:
        return JSONResponse(status_code=404, content={"message": f"unknown job {body.job!r}"})
    tracking.set_setting(job["setting"], "true" if body.enabled else "false")
    return JSONResponse(content={"job": body.job, "enabled": body.enabled})


@app.get("/api/automation/logs")
def automation_logs(limit: int = 50) -> dict:
    """Recent scheduled-job runs, newest first (durable — from job_runs)."""
    return {"runs": tracking.recent_job_runs(min(max(limit, 1), 200))}


app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
