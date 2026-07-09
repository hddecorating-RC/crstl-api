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


def _get_client() -> CrstlClient:
    return CrstlClient(
        base_url=os.environ.get("CRSTL_BASE_URL", "https://api.crstl.ai/v2"),
        email=os.environ.get("CRSTL_EMAIL", ""),
        password=os.environ.get("CRSTL_PASSWORD", ""),
        org_id=os.environ.get("CRSTL_ORG_ID", ""),
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


def _refresh_cache() -> None:
    if os.environ.get("MOCK_DATA", "").lower() in ("1", "true", "yes"):
        with _cache_lock:
            _cache["invoices"] = _MOCK_INVOICES
            _cache["last_synced"] = datetime.now(timezone.utc).isoformat()
            _cache["status"] = "ok (mock)"
        return
    try:
        invoices = _get_client().fetch_invoices()
        with _cache_lock:
            _cache["invoices"] = invoices
            _cache["last_synced"] = datetime.now(timezone.utc).isoformat()
            _cache["status"] = "ok"
    except Exception as exc:
        with _cache_lock:
            _cache["status"] = f"error: {exc}"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    tracking.init_db()
    await asyncio.to_thread(_refresh_cache)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(_refresh_cache, "cron", hour=7, minute=0)
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="HD Decorating Invoice Dashboard", lifespan=lifespan)


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
        content={"message": "NetSuite connector not yet configured"},
    )


app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
