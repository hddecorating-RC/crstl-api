import asyncio
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

app = FastAPI(title="HD Decorating Invoice Dashboard")


def _refresh_cache() -> None:
    try:
        invoices = _get_client().fetch_invoices()
        with _cache_lock:
            _cache["invoices"] = invoices
            _cache["last_synced"] = datetime.now(timezone.utc).isoformat()
            _cache["status"] = "ok"
    except Exception as exc:
        with _cache_lock:
            _cache["status"] = f"error: {exc}"


@app.on_event("startup")
async def startup() -> None:
    await asyncio.to_thread(_refresh_cache)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(_refresh_cache, "cron", hour=7, minute=0)
    scheduler.start()


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
