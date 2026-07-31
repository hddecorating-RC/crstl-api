import asyncio
import contextlib
import os
import pathlib
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import BaseModel

from app.crstl import CrstlClient
from app.export import build_csv
from app import tracking
from app.mail import send_mail, MailConfigError
from app.netsuite import transform_invoice
from app.netsuite_csv import build_netsuite_csv


def load_env(path: str = ".env") -> None:
    env_file = pathlib.Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


load_env()

_cache: dict = {"invoices": [], "last_synced": None, "status": "never"}
_cache_lock = threading.Lock()
_netsuite_state: dict = {"last_generated": None, "path": None, "count": 0, "skipped": 0, "error": None, "generating": False}
_netsuite_lock = threading.Lock()
_digest_state: dict = {"last_sent": None, "count": 0, "error": None, "sending": False}
_digest_lock = threading.Lock()


def _build_mock_po_provinces() -> dict[str, dict]:
    provinces = ["ON", "BC", "QC", "AB", "SK", "MB", "NS", "NB", "NL", "PE", "NT", "YT", "NU"]
    result = {}
    for i in range(50):
        po = f"PO-{98801 - i * 3}"
        if i % 10 < 2:
            result[po] = {"province": "ON", "store": "VAUGHAN"}
        elif i % 10 < 4:
            result[po] = {"province": "AB", "store": "CALGARY"}
        else:
            result[po] = {"province": provinces[i % len(provinces)], "store": None}
    return result


# Province map for MOCK_DATA mode (cycles through stores + provinces for 50 mock invoices)
_MOCK_PO_PROVINCES = _build_mock_po_provinces()


def _get_client() -> CrstlClient:
    return CrstlClient(
        base_url=os.environ.get("CRSTL_BASE_URL", "https://api.crstl.so/v2"),
        api_key=os.environ.get("CRSTL_API_KEY", ""),
    )

def _generate_mock_invoices(count: int = 50) -> list[dict]:
    _SERVICES = [
        ("Window Treatment Installation", 350.00),
        ("Drapery Installation", 325.00),
        ("Motorized Blinds Installation", 600.00),
        ("Sheer Curtain Installation", 200.00),
        ("Custom Roller Shades", 350.00),
        ("Roman Shade Installation", 275.00),
        ("Vertical Blind Installation", 180.00),
        ("Cornice Board Installation", 420.00),
        ("Plantation Shutter Installation", 550.00),
        ("Solar Screen Installation", 230.00),
    ]
    _PARTNERS = ["Home Depot", "Lowe's", "Costco"]
    _STATUSES = ["Open", "Open", "Open", "Completed"]  # 3:1 open:completed ratio
    _HARDWARE = ("Hardware / Materials", 1, 85.00)

    invoices = []
    base_inv = 2025_041
    base_po = 98_801
    # Spread invoices over the past ~10 weeks (one roughly every 1.5 days)
    base_day = date(2026, 7, 8)

    for i in range(count):
        idx = i + 1
        inv_date = base_day - timedelta(days=i + (i // 7))  # small gaps weekends
        due_date = inv_date + timedelta(days=30)
        created_hour = 8 + (idx % 10)
        service_idx = i % len(_SERVICES)
        svc_name, unit_price = _SERVICES[service_idx]
        qty = 2 + (i % 18)
        line_amount = round(unit_price * qty, 2)
        hw_amount = round(_HARDWARE[2] * (1 + i % 3), 2)
        subtotal = round(line_amount + hw_amount, 2)
        tax = round(subtotal * 0.075, 2)
        total = round(subtotal + tax, 2)
        invoices.append({
            "transaction_id": f"mock-{idx:03d}",
            "invoice_number": f"INV-2025-{base_inv - i:03d}",
            "po_number": f"PO-{base_po - i * 3}",
            "trading_partner": _PARTNERS[i % len(_PARTNERS)],
            "invoice_date": inv_date.isoformat(),
            "due_date": due_date.isoformat(),
            "status": _STATUSES[i % len(_STATUSES)],
            "subtotal": subtotal,
            "tax_amount": tax,
            "total_amount": total,
            "currency": "USD",
            "created_at": f"{inv_date.isoformat()}T{created_hour:02d}:00:00Z",
            "invoice_lines": [
                {"description": svc_name, "quantity": qty, "line_amount": line_amount},
                {"description": _HARDWARE[0], "quantity": _HARDWARE[1], "line_amount": hw_amount},
            ],
        })
    return invoices


_MOCK_INVOICES = _generate_mock_invoices(50)


def _attach_provinces(invoices: list[dict], po_provinces: dict[str, dict]) -> None:
    """Attach ship-to province + store from the source 850 to each invoice (factual, no rate math)."""
    for inv in invoices:
        loc = po_provinces.get(inv.get("po_number", ""), {})
        inv["province"] = loc.get("province")
        inv["store"] = loc.get("store")


def _refresh_cache() -> None:
    if os.environ.get("MOCK_DATA", "").lower() in ("1", "true", "yes"):
        invoices = list(_MOCK_INVOICES)
        _attach_provinces(invoices, _MOCK_PO_PROVINCES)
        with _cache_lock:
            _cache["invoices"] = invoices
            _cache["last_synced"] = datetime.now(timezone.utc).isoformat()
            _cache["status"] = "ok (mock)"
        return
    try:
        client = _get_client()
        invoices = client.fetch_invoices()
        po_provinces = client.fetch_po_provinces()
        _attach_provinces(invoices, po_provinces)
        with _cache_lock:
            _cache["invoices"] = invoices
            _cache["last_synced"] = datetime.now(timezone.utc).isoformat()
            _cache["status"] = "ok"
    except Exception as exc:
        with _cache_lock:
            _cache["status"] = f"error: {exc}"


def _generate_netsuite_export() -> None:
    """Build NetSuite CSV from the cached invoices. Province/store are already
    attached to each invoice by `_refresh_cache` — no extra API round-trips."""
    with _cache_lock:
        invoices = list(_cache["invoices"])

    if not invoices:
        print("NetSuite export: cache empty, skipping")
        return

    records, skipped = [], []
    for inv in invoices:
        line_items = transform_invoice(inv, province=inv.get("province"), store=inv.get("store"))
        if line_items is None:
            skipped.append(inv.get("po_number", "?"))
        else:
            records.extend(line_items)

    if skipped:
        print(f"WARNING: NetSuite export skipped {len(skipped)} invoices (no province mapping): {skipped}")

    csv_bytes = build_netsuite_csv(records)
    out_path = pathlib.Path(__file__).parent.parent / ".tmp" / f"netsuite_export_{date.today().isoformat()}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(csv_bytes)

    with _netsuite_lock:
        _netsuite_state["last_generated"] = datetime.now(timezone.utc).isoformat()
        _netsuite_state["path"] = str(out_path)
        _netsuite_state["count"] = len(records)
        _netsuite_state["skipped"] = len(skipped)
        _netsuite_state["error"] = None

    print(f"NetSuite export: {len(records)} invoices → {out_path} ({len(skipped)} skipped)")


def _send_daily_digest(selected_ids: list[str] | None = None) -> dict:
    """Send an invoice email.

    Two modes:
    - Digest (selected_ids is None): pull invoices not yet emailed. Always
      sends, even on zero new invoices — accounting uses receipt as proof
      the pipeline is alive.
    - Selection (selected_ids provided): send exactly those invoices,
      regardless of whether they've been emailed before.

    In both modes, sent invoices are marked with an 'emailed' tracking event
    so they don't appear in future digests.

    Returns a summary dict. Raises MailConfigError if env vars missing.
    """
    recipients_raw = os.environ.get("MAIL_RECIPIENTS", "")
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    if not recipients:
        raise MailConfigError("MAIL_RECIPIENTS not set (comma-separated addresses)")

    with _cache_lock:
        invoices = list(_cache["invoices"])

    if selected_ids is not None:
        selected_set = set(selected_ids)
        to_send = [inv for inv in invoices if inv["transaction_id"] in selected_set]
        mode = "selection"
    else:
        all_ids = [inv["transaction_id"] for inv in invoices]
        new_ids = set(tracking.get_unemailed_ids(all_ids))
        to_send = [inv for inv in invoices if inv["transaction_id"] in new_ids]
        mode = "digest"

    today = date.today().isoformat()
    if to_send:
        total = sum(inv.get("total_amount", 0) for inv in to_send)
        by_province: dict[str, int] = {}
        for inv in to_send:
            p = inv.get("province") or "—"
            by_province[p] = by_province.get(p, 0) + 1
        prov_rows = "".join(f"<tr><td>{p}</td><td style='text-align:right'>{c}</td></tr>" for p, c in sorted(by_province.items()))

        if mode == "selection":
            subject = f"HD Invoices — {today} — {len(to_send)} selected"
            intro = f"<p>{len(to_send)} invoice(s) sent manually from the dashboard.</p>"
        else:
            subject = f"HD Invoice Digest — {today} — {len(to_send)} new"
            intro = f"<p>{len(to_send)} new invoice(s) since the last digest.</p>"

        body_html = f"""
            {intro}
            <p><strong>Total value:</strong> ${total:,.2f} CAD</p>
            <table cellpadding="4" cellspacing="0" border="1" style="border-collapse:collapse">
              <tr><th align="left">Province</th><th align="right">Count</th></tr>
              {prov_rows}
            </table>
            <p>Full details attached as CSV.</p>
        """
        csv_bytes = build_csv(to_send)
        attachments = [(f"hd_invoices_{today}.csv", csv_bytes, "text/csv")]
    else:
        # Only reachable in digest mode — selection mode with 0 matches is a caller bug
        subject = f"HD Invoice Digest — {today} — 0 new"
        body_html = "<p>No new invoices since the last digest. Pipeline is healthy.</p>"
        attachments = None

    send_mail(subject=subject, body_html=body_html, recipients=recipients, attachments=attachments)

    if to_send:
        tracking.record_events([inv["transaction_id"] for inv in to_send], "emailed")

    return {"sent_to": recipients, "count": len(to_send), "subject": subject, "mode": mode}


def _run_daily_digest_job() -> None:
    """Scheduler entry point — wraps _send_daily_digest with state tracking."""
    with _digest_lock:
        if _digest_state.get("sending"):
            print("Digest: already running, skipping")
            return
        _digest_state["sending"] = True
    try:
        result = _send_daily_digest()
        with _digest_lock:
            _digest_state["last_sent"] = datetime.now(timezone.utc).isoformat()
            _digest_state["count"] = result["count"]
            _digest_state["error"] = None
        print(f"Digest: sent {result['count']} invoices to {result['sent_to']}")
    except Exception as exc:
        with _digest_lock:
            _digest_state["error"] = str(exc)
        print(f"WARNING: digest send failed: {exc}")
    finally:
        with _digest_lock:
            _digest_state["sending"] = False


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    tracking.init_db()
    await asyncio.to_thread(_refresh_cache)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(_refresh_cache, "cron", hour=7, minute=0, timezone="America/Toronto")
    scheduler.add_job(_generate_netsuite_export, "cron", hour=4, minute=0, timezone="America/Toronto")
    scheduler.add_job(_run_daily_digest_job, "cron", hour=7, minute=15, timezone="America/Toronto")
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="HD Decorating Invoice Dashboard", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict:
    """Lightweight liveness probe for container orchestrators. Does not touch
    the cache lock or the Crstl API."""
    return {"status": "ok"}


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
            {**inv, **events.get(inv["transaction_id"], {"exported_at": None, "netsuite_at": None})}
            for inv in invoices
        ]

    return {"invoices": invoices, "last_synced": last_synced, "status": status}


@app.post("/api/sync")
def sync() -> dict:
    _refresh_cache()
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
    filename = f"invoices_{date.today().isoformat()}.csv"
    csv_bytes = build_csv(invoices, ids=body.ids)

    cache_ids = {inv["transaction_id"] for inv in invoices}
    exported_ids = (
        [i for i in body.ids if i in cache_ids]
        if body.ids is not None
        else [inv["transaction_id"] for inv in invoices]
    )
    tracking.record_events(exported_ids, "exported")

    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/netsuite")
def netsuite() -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={"message": "NetSuite REST connector not yet configured. Use CSV export path."},
    )


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
        await asyncio.to_thread(_generate_netsuite_export)
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
        result = await asyncio.to_thread(_send_daily_digest, body.ids)
    except MailConfigError as exc:
        return JSONResponse(status_code=400, content={"message": str(exc)})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"message": str(exc)})
    return JSONResponse(result)


@app.get("/api/email/status")
def email_status() -> dict:
    with _digest_lock:
        state = {**_digest_state}
    # Fall back to the tracking DB after a restart wipes in-memory state.
    # Only surfaces sends that actually marked invoices — a 0-count heartbeat
    # right before restart won't be recoverable.
    if state.get("last_sent") is None:
        state["last_sent"] = tracking.latest_event_time("emailed")
    return state


app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
