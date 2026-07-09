import asyncio
import contextlib
import os
import pathlib
import threading
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import BaseModel

from app.crstl import CrstlClient
from app.export import build_csv


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

_MOCK_INVOICES = [
    {
        "transaction_id": "mock-001",
        "invoice_number": "INV-2025-041",
        "po_number": "PO-98801",
        "trading_partner": "Home Depot",
        "invoice_date": "2026-07-08",
        "due_date": "2026-08-07",
        "status": "Open",
        "subtotal": 4180.00,
        "tax_amount": 320.00,
        "total_amount": 4500.00,
        "currency": "USD",
        "created_at": "2026-07-08T10:00:00Z",
        "invoice_lines": [
            {"description": "Window Treatment Installation", "quantity": 10, "line_amount": 3500.00},
            {"description": "Hardware / Materials", "quantity": 1, "line_amount": 680.00},
        ],
    },
    {
        "transaction_id": "mock-002",
        "invoice_number": "INV-2025-040",
        "po_number": "PO-98756",
        "trading_partner": "Home Depot",
        "invoice_date": "2026-07-07",
        "due_date": "2026-08-06",
        "status": "Completed",
        "subtotal": 1950.00,
        "tax_amount": 150.00,
        "total_amount": 2100.00,
        "currency": "USD",
        "created_at": "2026-07-07T09:30:00Z",
        "invoice_lines": [
            {"description": "Drapery Installation", "quantity": 6, "line_amount": 1950.00},
        ],
    },
    {
        "transaction_id": "mock-003",
        "invoice_number": "INV-2025-039",
        "po_number": "PO-98734",
        "trading_partner": "Home Depot",
        "invoice_date": "2026-07-06",
        "due_date": "2026-08-05",
        "status": "Open",
        "subtotal": 6320.00,
        "tax_amount": 480.00,
        "total_amount": 6800.00,
        "currency": "USD",
        "created_at": "2026-07-06T14:15:00Z",
        "invoice_lines": [
            {"description": "Motorized Blinds Installation", "quantity": 8, "line_amount": 4800.00},
            {"description": "Control System Setup", "quantity": 1, "line_amount": 1520.00},
        ],
    },
    {
        "transaction_id": "mock-004",
        "invoice_number": "INV-2025-038",
        "po_number": "PO-98712",
        "trading_partner": "Home Depot",
        "invoice_date": "2026-07-03",
        "due_date": "2026-08-02",
        "status": "Completed",
        "subtotal": 2800.00,
        "tax_amount": 200.00,
        "total_amount": 3000.00,
        "currency": "USD",
        "created_at": "2026-07-03T08:00:00Z",
        "invoice_lines": [
            {"description": "Sheer Curtain Installation", "quantity": 14, "line_amount": 2800.00},
        ],
    },
    {
        "transaction_id": "mock-005",
        "invoice_number": "INV-2025-037",
        "po_number": "PO-98690",
        "trading_partner": "Home Depot",
        "invoice_date": "2026-07-01",
        "due_date": "2026-07-31",
        "status": "Open",
        "subtotal": 9100.00,
        "tax_amount": 700.00,
        "total_amount": 9800.00,
        "currency": "USD",
        "created_at": "2026-07-01T11:00:00Z",
        "invoice_lines": [
            {"description": "Custom Roller Shades", "quantity": 20, "line_amount": 7000.00},
            {"description": "Fascia / Valance Trim", "quantity": 20, "line_amount": 2100.00},
        ],
    },
]


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
        return {**_cache}


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
