# Invoice Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a FastAPI + Tailwind + Alpine.js internal dashboard that shows Crstl EDI 810 invoice status, supports search/filter/multi-select, streams CSV exports, and is deployable as a Docker container on Proxmox.

**Architecture:** FastAPI serves a single-page HTML UI and four API endpoints. Invoice data is fetched from the Crstl v2 API, normalized, and held in an in-memory cache refreshed daily at 7am via APScheduler. The frontend is pure HTML with Tailwind CDN and Alpine.js — no build step.

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, APScheduler, Requests, pytest, httpx (test client)

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `requirements.txt` | Modify | Add FastAPI, Uvicorn, APScheduler, httpx |
| `app/__init__.py` | Create | Package marker |
| `app/crstl.py` | Create | Crstl auth, token cache, fetch + normalize invoices |
| `app/export.py` | Create | Generate CSV bytes from invoice list |
| `app/main.py` | Create | FastAPI app, routes, in-memory cache, APScheduler |
| `app/static/index.html` | Create | Full single-page UI (Tailwind CDN + Alpine.js) |
| `Dockerfile` | Create | Container build |
| `docker-compose.yml` | Create | Local + Proxmox deploy |
| `tests/__init__.py` | Create | Package marker |
| `tests/test_crstl.py` | Create | Unit tests for Crstl client |
| `tests/test_export.py` | Create | Unit tests for CSV generation |
| `tests/test_api.py` | Create | Integration tests for API endpoints |

---

## Task 1: Dependencies & Scaffold

**Files:**
- Modify: `requirements.txt`
- Create: `app/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: Update requirements.txt**

```
requests>=2.32
fastapi>=0.111
uvicorn[standard]>=0.30
apscheduler>=3.10
httpx>=0.27
pytest>=8.0
gspread>=6.0
gspread-formatting>=1.1
```

- [ ] **Step 2: Create package markers**

Create `app/__init__.py` — empty file.
Create `tests/__init__.py` — empty file.

- [ ] **Step 3: Install dependencies**

```bash
pip install -r requirements.txt
```

Expected: all packages install without error.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt app/__init__.py tests/__init__.py
git commit -m "chore: add fastapi, apscheduler, httpx dependencies"
```

---

## Task 2: Crstl Client

**Files:**
- Create: `app/crstl.py`
- Create: `tests/test_crstl.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_crstl.py`:

```python
import json
import pathlib
import pytest
from unittest.mock import patch, MagicMock
from app.crstl import CrstlClient


SAMPLE_INVOICES = [
    {
        "id": "tx-001",
        "reference_id": "INV-2025-001",
        "trading_partner_name": "Home Depot",
        "state": "Open",
        "created_at": "2026-07-01T00:00:00Z",
        "transaction_data": {
            "invoice_number": "INV-2025-001",
            "invoice_date": "2026-07-01",
            "due_date": "2026-08-01",
            "total_amount": 4500.00,
            "currency": "USD",
            "invoice_lines": [
                {"line_amount": 4180.00},
            ],
            "tax_amount": 320.00,
        },
    }
]


@patch("app.crstl.requests.post")
def test_authenticate_stores_token(mock_post, tmp_path):
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {
        "access_token": "tok123",
        "refresh_token": "ref456",
        "access_token_expires_at": "2099-01-01T00:00:00Z",
    }
    client = CrstlClient(
        base_url="https://api.crstl.ai/v2",
        email="a@b.com",
        password="pass",
        token_cache_path=str(tmp_path / "tokens.json"),
    )
    token = client.get_access_token()
    assert token == "tok123"
    assert (tmp_path / "tokens.json").exists()


@patch("app.crstl.requests.post")
def test_reuses_cached_token(mock_post, tmp_path):
    cache = tmp_path / "tokens.json"
    cache.write_text(json.dumps({
        "access_token": "cached_tok",
        "refresh_token": "ref",
        "access_token_expires_at": "2099-01-01T00:00:00Z",
    }))
    client = CrstlClient(
        base_url="https://api.crstl.ai/v2",
        email="a@b.com",
        password="pass",
        token_cache_path=str(cache),
    )
    token = client.get_access_token()
    assert token == "cached_tok"
    mock_post.assert_not_called()


def test_extract_invoice_fields():
    client = CrstlClient(
        base_url="https://api.crstl.ai/v2",
        email="a@b.com",
        password="pass",
        token_cache_path="/tmp/tok.json",
    )
    detail = SAMPLE_INVOICES[0]
    result = client._extract_invoice_fields(detail)
    assert result["invoice_number"] == "INV-2025-001"
    assert result["status"] == "Open"
    assert result["total_amount"] == 4500.0
    assert result["tax_amount"] == 320.0
    assert result["subtotal"] == 4180.0
    assert result["transaction_id"] == "tx-001"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_crstl.py -v
```

Expected: `ImportError: cannot import name 'CrstlClient' from 'app.crstl'`

- [ ] **Step 3: Create app/crstl.py**

```python
import json
import os
import pathlib
from datetime import datetime, timezone

import requests


class CrstlClient:
    def __init__(self, base_url: str, email: str, password: str, org_id: str = "", token_cache_path: str = ".tmp/crstl_tokens.json"):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.org_id = org_id
        self.token_cache_path = pathlib.Path(token_cache_path)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def get_access_token(self) -> str:
        tokens = self._load_cached_tokens()
        if self._token_is_valid(tokens):
            return tokens["access_token"]

        if tokens.get("refresh_token"):
            refreshed = self._refresh(tokens["refresh_token"])
            if refreshed:
                return refreshed

        return self._login()

    def _load_cached_tokens(self) -> dict:
        if self.token_cache_path.exists():
            try:
                return json.loads(self.token_cache_path.read_text())
            except Exception:
                pass
        return {}

    def _save_tokens(self, tokens: dict) -> None:
        self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_cache_path.write_text(json.dumps(tokens, indent=2))

    def _token_is_valid(self, tokens: dict) -> bool:
        expires_at = tokens.get("access_token_expires_at")
        if not expires_at or not tokens.get("access_token"):
            return False
        try:
            exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            return exp > datetime.now(timezone.utc)
        except Exception:
            return False

    def _refresh(self, refresh_token: str) -> str | None:
        resp = requests.post(
            f"{self.base_url}/auth/refresh",
            json={"refresh_token": refresh_token},
            timeout=30,
        )
        if resp.status_code == 200:
            tokens = resp.json()
            self._save_tokens(tokens)
            return tokens["access_token"]
        return None

    def _login(self) -> str:
        payload = {"email": self.email, "password": self.password}
        if self.org_id:
            payload["organization_id"] = self.org_id
        resp = requests.post(f"{self.base_url}/auth/token", json=payload, timeout=30)
        resp.raise_for_status()
        tokens = resp.json()
        self._save_tokens(tokens)
        return tokens["access_token"]

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def fetch_invoices(self) -> list[dict]:
        token = self.get_access_token()
        transactions = self._fetch_all_transactions(token)
        invoices = []
        for tx in transactions:
            tx_id = tx.get("id") or tx.get("transaction_id")
            if not tx_id:
                continue
            try:
                detail = self._fetch_transaction_detail(tx_id, token)
                invoices.append(self._extract_invoice_fields(detail))
            except requests.exceptions.HTTPError:
                pass
        return invoices

    def _fetch_all_transactions(self, token: str, transaction_type: str = "810") -> list:
        results = []
        params = {"transaction_type": transaction_type, "limit": 100}
        cursor = None
        while True:
            if cursor:
                params["cursor"] = cursor
            resp = requests.get(
                f"{self.base_url}/transaction",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            inner = data.get("data", data)
            records = inner.get("transactions") or inner.get("results") or inner.get("items") or []
            results.extend(records)
            cursor = inner.get("next_cursor")
            if not cursor or len(records) < 100:
                break
        return results

    def _fetch_transaction_detail(self, transaction_id: str, token: str) -> dict:
        resp = requests.get(
            f"{self.base_url}/transaction/{transaction_id}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", data)

    def _extract_invoice_fields(self, detail: dict) -> dict:
        tx_data = detail.get("transaction_data") or detail.get("document_data") or {}

        def first(*keys):
            for k in keys:
                v = tx_data.get(k) or detail.get(k)
                if v is not None and v != "":
                    return v
            return None

        total_amount = self._parse_float(first("total_amount", "invoice_amount", "amount"))

        lines = tx_data.get("invoice_lines") or tx_data.get("line_items") or []
        subtotal = sum(
            self._parse_float(line.get("line_amount") or line.get("amount") or line.get("extended_amount"))
            for line in lines
        )

        tax_raw = first("tax_amount", "tax_total", "total_tax")
        tax_amount = self._parse_float(tax_raw)

        if tax_amount == 0.0 and subtotal > 0.0 and total_amount > subtotal:
            tax_amount = round(total_amount - subtotal, 2)
        if subtotal == 0.0:
            subtotal = round(total_amount - tax_amount, 2)

        return {
            "transaction_id":  str(detail.get("id") or detail.get("transaction_id") or ""),
            "invoice_number":  str(first("invoice_number", "document_number", "number") or ""),
            "po_number":       str(detail.get("reference_id") or first("purchase_order_number", "po_number") or ""),
            "trading_partner": str(detail.get("trading_partner_name") or first("trading_partner", "retailer") or ""),
            "invoice_date":    str(first("invoice_date", "issue_date", "date") or ""),
            "due_date":        str(first("due_date", "payment_due_date") or ""),
            "status":          str(detail.get("state") or detail.get("status") or first("status") or ""),
            "subtotal":        round(subtotal, 2),
            "tax_amount":      round(tax_amount, 2),
            "total_amount":    round(total_amount, 2),
            "currency":        str(first("currency", "currency_code") or "USD"),
            "created_at":      str(detail.get("created_at") or ""),
            "invoice_lines":   lines,
        }

    @staticmethod
    def _parse_float(val) -> float:
        if val is None:
            return 0.0
        try:
            return float(str(val).replace(",", ""))
        except (ValueError, TypeError):
            return 0.0
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_crstl.py -v
```

Expected: 4 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/crstl.py tests/test_crstl.py
git commit -m "feat: add Crstl API client with auth, token cache, invoice fetch"
```

---

## Task 3: Export Module

**Files:**
- Create: `app/export.py`
- Create: `tests/test_export.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_export.py`:

```python
import io
import csv
from app.export import build_csv

INVOICES = [
    {
        "invoice_number": "INV-001",
        "po_number": "PO-123",
        "trading_partner": "Home Depot",
        "invoice_date": "2026-07-01",
        "due_date": "2026-08-01",
        "status": "Open",
        "subtotal": 4180.0,
        "tax_amount": 320.0,
        "total_amount": 4500.0,
        "currency": "USD",
        "transaction_id": "tx-001",
        "created_at": "2026-07-01T00:00:00Z",
        "invoice_lines": [],
    },
    {
        "invoice_number": "INV-002",
        "po_number": "PO-456",
        "trading_partner": "Home Depot",
        "invoice_date": "2026-07-02",
        "due_date": "2026-08-02",
        "status": "Completed",
        "subtotal": 1950.0,
        "tax_amount": 150.0,
        "total_amount": 2100.0,
        "currency": "USD",
        "transaction_id": "tx-002",
        "created_at": "2026-07-02T00:00:00Z",
        "invoice_lines": [],
    },
]


def test_build_csv_returns_bytes():
    result = build_csv(INVOICES)
    assert isinstance(result, bytes)


def test_build_csv_has_header():
    result = build_csv(INVOICES)
    reader = csv.reader(io.StringIO(result.decode("utf-8")))
    header = next(reader)
    assert "Invoice Number" in header
    assert "Status" in header
    assert "Tax Amount" in header


def test_build_csv_row_count():
    result = build_csv(INVOICES)
    reader = csv.reader(io.StringIO(result.decode("utf-8")))
    rows = list(reader)
    assert len(rows) == 3  # header + 2 data rows


def test_build_csv_subset_by_ids():
    result = build_csv(INVOICES, ids=["tx-001"])
    reader = csv.reader(io.StringIO(result.decode("utf-8")))
    rows = list(reader)
    assert len(rows) == 2  # header + 1 row
    assert rows[1][0] == "INV-001"


def test_build_csv_empty():
    result = build_csv([])
    reader = csv.reader(io.StringIO(result.decode("utf-8")))
    rows = list(reader)
    assert len(rows) == 1  # header only
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_export.py -v
```

Expected: `ImportError: cannot import name 'build_csv' from 'app.export'`

- [ ] **Step 3: Create app/export.py**

```python
import csv
import io

COLUMNS = [
    ("Invoice Number",  "invoice_number"),
    ("PO Number",       "po_number"),
    ("Trading Partner", "trading_partner"),
    ("Invoice Date",    "invoice_date"),
    ("Due Date",        "due_date"),
    ("Status",          "status"),
    ("Subtotal",        "subtotal"),
    ("Tax Amount",      "tax_amount"),
    ("Total Amount",    "total_amount"),
    ("Currency",        "currency"),
    ("Transaction ID",  "transaction_id"),
    ("Created At",      "created_at"),
]


def build_csv(invoices: list[dict], ids: list[str] | None = None) -> bytes:
    """Return CSV bytes for invoices. Pass ids to export a subset by transaction_id."""
    if ids is not None:
        invoices = [inv for inv in invoices if inv.get("transaction_id") in ids]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=[col for col, _ in COLUMNS])
    writer.writeheader()
    for inv in invoices:
        writer.writerow({col: inv.get(key, "") for col, key in COLUMNS})
    return buf.getvalue().encode("utf-8")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_export.py -v
```

Expected: 5 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/export.py tests/test_export.py
git commit -m "feat: add CSV export builder with optional id subset filter"
```

---

## Task 4: FastAPI App

**Files:**
- Create: `app/main.py`
- Create: `tests/test_api.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_api.py`:

```python
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

MOCK_INVOICES = [
    {
        "transaction_id": "tx-001",
        "invoice_number": "INV-001",
        "po_number": "PO-123",
        "trading_partner": "Home Depot",
        "invoice_date": "2026-07-01",
        "due_date": "2026-08-01",
        "status": "Open",
        "subtotal": 4180.0,
        "tax_amount": 320.0,
        "total_amount": 4500.0,
        "currency": "USD",
        "created_at": "2026-07-01T00:00:00Z",
        "invoice_lines": [],
    }
]


@pytest.fixture
def client():
    with patch("app.main.CrstlClient") as MockClient:
        mock_instance = MagicMock()
        mock_instance.fetch_invoices.return_value = MOCK_INVOICES
        MockClient.return_value = mock_instance

        from app.main import app
        with TestClient(app) as c:
            yield c


def test_get_invoices_returns_list(client):
    resp = client.get("/api/invoices")
    assert resp.status_code == 200
    data = resp.json()
    assert "invoices" in data
    assert "last_synced" in data
    assert "status" in data


def test_sync_triggers_refresh(client):
    resp = client.post("/api/sync")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_export_all_returns_csv(client):
    # Pre-populate cache via sync
    client.post("/api/sync")
    resp = client.post("/api/export", json={})
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]


def test_export_subset_by_ids(client):
    client.post("/api/sync")
    resp = client.post("/api/export", json={"ids": ["tx-001"]})
    assert resp.status_code == 200
    lines = resp.content.decode().splitlines()
    assert len(lines) == 2  # header + 1 row


def test_export_empty_cache_returns_503(client):
    # Don't sync — cache is empty
    resp = client.post("/api/export", json={})
    assert resp.status_code == 503


def test_netsuite_returns_501(client):
    resp = client.post("/api/netsuite")
    assert resp.status_code == 501
    assert "not yet configured" in resp.json()["message"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_api.py -v
```

Expected: `ImportError: cannot import name 'app' from 'app.main'`

- [ ] **Step 3: Create app/main.py**

```python
import os
import pathlib
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse
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

_client = CrstlClient(
    base_url=os.environ.get("CRSTL_BASE_URL", "https://api.crstl.ai/v2"),
    email=os.environ.get("CRSTL_EMAIL", ""),
    password=os.environ.get("CRSTL_PASSWORD", ""),
    org_id=os.environ.get("CRSTL_ORG_ID", ""),
)

_cache: dict = {"invoices": [], "last_synced": None, "status": "never"}

app = FastAPI(title="HD Decorating Invoice Dashboard")


def _refresh_cache() -> None:
    try:
        invoices = _client.fetch_invoices()
        _cache["invoices"] = invoices
        _cache["last_synced"] = datetime.now(timezone.utc).isoformat()
        _cache["status"] = "ok"
    except Exception as exc:
        _cache["status"] = f"error: {exc}"


@app.on_event("startup")
async def startup() -> None:
    _refresh_cache()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(_refresh_cache, "cron", hour=7, minute=0)
    scheduler.start()


@app.get("/api/invoices")
def get_invoices() -> dict:
    return {
        "invoices": _cache["invoices"],
        "last_synced": _cache["last_synced"],
        "status": _cache["status"],
    }


@app.post("/api/sync")
def sync() -> dict:
    _refresh_cache()
    return {"ok": True, "last_synced": _cache["last_synced"], "status": _cache["status"]}


class ExportRequest(BaseModel):
    ids: Optional[list[str]] = None


@app.post("/api/export")
def export(body: ExportRequest = ExportRequest()) -> Response:
    if not _cache["invoices"]:
        return JSONResponse(
            status_code=503,
            content={"message": "Cache is empty. Trigger /api/sync first."},
        )
    from datetime import date
    filename = f"invoices_{date.today().isoformat()}.csv"
    csv_bytes = build_csv(_cache["invoices"], ids=body.ids)
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
```

- [ ] **Step 4: Create app/static/ directory**

```bash
mkdir -p app/static
```

Create a temporary placeholder `app/static/index.html`:

```html
<!DOCTYPE html><html><body><p>Loading...</p></body></html>
```

This allows the `StaticFiles` mount to initialise without error during tests.

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_api.py -v
```

Expected: 6 tests PASSED.

- [ ] **Step 6: Commit**

```bash
git add app/main.py app/static/index.html tests/test_api.py
git commit -m "feat: add FastAPI app with invoice cache, export, sync, and NetSuite stub"
```

---

## Task 5: Frontend UI

**Files:**
- Modify: `app/static/index.html`

- [ ] **Step 1: Replace placeholder with full UI**

Write `app/static/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>HD Decorating · Invoice Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
  <style>
    [x-cloak] { display: none !important; }
    .row-selected { background-color: #eff6ff; }
    .row-active { border-left: 3px solid #2563eb; }
  </style>
</head>
<body class="bg-gray-50 text-gray-800 min-h-screen" x-data="dashboard()" x-init="init()" x-cloak>

  <!-- Toast -->
  <div x-show="toast" x-transition class="fixed top-4 right-4 bg-green-600 text-white px-4 py-2 rounded shadow-lg z-50 text-sm" x-text="toast"></div>

  <!-- Top bar -->
  <div class="bg-slate-900 text-slate-100 px-6 py-3 flex items-center justify-between">
    <span class="font-semibold text-sm">HD Decorating &middot; Invoice Dashboard</span>
    <div class="flex items-center gap-3 text-xs text-slate-400">
      <span x-show="lastSynced">Last sync: <span x-text="fmtTime(lastSynced)"></span></span>
      <span x-show="!lastSynced">Never synced</span>
      <span :class="syncStatus === 'ok' ? 'text-green-400' : 'text-red-400'" x-text="syncStatus === 'ok' ? '● Live' : '● Error'"></span>
    </div>
  </div>

  <div class="max-w-screen-xl mx-auto px-6 py-5">

    <!-- Stat cards -->
    <div class="grid grid-cols-4 gap-4 mb-5">
      <div class="bg-white rounded-lg border border-gray-200 p-4 cursor-pointer hover:border-amber-400 transition"
           :class="activeFilter === 'Draft' ? 'ring-2 ring-amber-400' : ''"
           @click="activeFilter = activeFilter === 'Draft' ? 'All' : 'Draft'">
        <div class="text-xs uppercase tracking-wide text-gray-400 mb-1">Draft</div>
        <div class="text-3xl font-bold text-amber-500" x-text="counts.Draft"></div>
        <div class="text-xs text-gray-400 mt-1">Not yet sent</div>
      </div>
      <div class="bg-white rounded-lg border border-gray-200 p-4 cursor-pointer hover:border-green-400 transition"
           :class="activeFilter === 'Sent' ? 'ring-2 ring-green-400' : ''"
           @click="activeFilter = activeFilter === 'Sent' ? 'All' : 'Sent'">
        <div class="text-xs uppercase tracking-wide text-gray-400 mb-1">Sent</div>
        <div class="text-3xl font-bold text-green-500" x-text="counts.Sent"></div>
        <div class="text-xs text-gray-400 mt-1">Transmitted to HD</div>
      </div>
      <div class="bg-white rounded-lg border border-gray-200 p-4">
        <div class="text-xs uppercase tracking-wide text-gray-400 mb-1">Total Value</div>
        <div class="text-3xl font-bold text-slate-800" x-text="fmtMoney(totalValue)"></div>
        <div class="text-xs text-gray-400 mt-1">All invoices</div>
      </div>
      <div class="bg-white rounded-lg border border-gray-200 p-4">
        <div class="text-xs uppercase tracking-wide text-gray-400 mb-1">Last Export</div>
        <div class="text-sm font-semibold text-slate-800 mt-1" x-text="lastExport ? fmtTime(lastExport) : '—'"></div>
        <div class="text-xs text-green-500 mt-1" x-show="lastExport">✓ Success</div>
      </div>
    </div>

    <!-- Toolbar -->
    <div class="flex items-center gap-3 mb-3">
      <div class="flex-1 relative">
        <span class="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 text-sm">🔍</span>
        <input type="text" x-model="search" placeholder="Search invoice #, PO #..."
               class="w-full pl-9 pr-4 py-2 text-sm border border-gray-200 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-blue-500">
      </div>
      <div class="flex gap-2">
        <template x-for="f in ['All','Draft','Sent']" :key="f">
          <button @click="activeFilter = f"
                  :class="activeFilter === f ? 'bg-blue-600 text-white' : 'bg-white text-gray-500 border border-gray-200 hover:border-blue-400'"
                  class="px-4 py-2 rounded-full text-xs font-medium transition" x-text="f"></button>
        </template>
      </div>
    </div>

    <!-- Multi-select bar -->
    <div x-show="selected.size > 0" x-transition
         class="flex items-center gap-3 bg-blue-50 border border-blue-200 rounded-lg px-4 py-2 mb-3">
      <span class="text-sm font-medium text-blue-700" x-text="`${selected.size} invoice${selected.size > 1 ? 's' : ''} selected`"></span>
      <button @click="exportCsv()" :disabled="exporting"
              class="bg-blue-600 hover:bg-blue-700 text-white text-xs font-medium px-3 py-1.5 rounded disabled:opacity-50">
        <span x-show="!exporting">↓ Export Selected</span>
        <span x-show="exporting">Exporting…</span>
      </button>
      <button disabled class="bg-white border border-blue-200 text-blue-300 text-xs px-3 py-1.5 rounded cursor-not-allowed">
        ⬡ Push to NetSuite <span class="ml-1 bg-gray-100 text-gray-400 text-xs px-1.5 py-0.5 rounded">soon</span>
      </button>
      <button @click="selected.clear(); selected = new Set()" class="ml-auto text-xs text-gray-400 hover:text-gray-600">✕ Clear</button>
    </div>

    <!-- Master / Detail -->
    <div class="flex gap-4" style="height: calc(100vh - 320px); min-height: 400px;">

      <!-- Master list -->
      <div class="flex-1 bg-white border border-gray-200 rounded-lg overflow-hidden flex flex-col">
        <!-- Table header -->
        <div class="grid text-xs font-semibold text-gray-400 uppercase tracking-wide px-4 py-2 border-b border-gray-100 bg-gray-50"
             style="grid-template-columns: 32px 1fr 1fr 100px 80px 90px">
          <span><input type="checkbox" @change="toggleAll($event)" class="w-3.5 h-3.5"></span>
          <span>Invoice #</span>
          <span>PO #</span>
          <span>Date</span>
          <span>Total</span>
          <span>Status</span>
        </div>
        <!-- Rows -->
        <div class="overflow-y-auto flex-1">
          <template x-if="loading">
            <div class="flex items-center justify-center h-32 text-gray-400 text-sm">Loading invoices…</div>
          </template>
          <template x-if="!loading && filteredInvoices.length === 0">
            <div class="flex items-center justify-center h-32 text-gray-400 text-sm">No invoices found</div>
          </template>
          <template x-for="inv in filteredInvoices" :key="inv.transaction_id">
            <div class="grid items-center px-4 py-2.5 border-b border-gray-50 cursor-pointer hover:bg-gray-50 text-sm transition"
                 style="grid-template-columns: 32px 1fr 1fr 100px 80px 90px"
                 :class="{'row-selected': selected.has(inv.transaction_id), 'row-active border-l-2 border-l-blue-500': activeInvoice?.transaction_id === inv.transaction_id}"
                 @click.self="selectInvoice(inv)">
              <span><input type="checkbox" :checked="selected.has(inv.transaction_id)"
                           @click.stop @change="toggleSelect(inv.transaction_id)" class="w-3.5 h-3.5"></span>
              <span class="font-medium text-slate-700 truncate" @click="selectInvoice(inv)" x-text="inv.invoice_number || inv.transaction_id"></span>
              <span class="text-gray-400 truncate" @click="selectInvoice(inv)" x-text="inv.po_number || '—'"></span>
              <span class="text-gray-400" @click="selectInvoice(inv)" x-text="inv.invoice_date || '—'"></span>
              <span class="font-medium" @click="selectInvoice(inv)" x-text="fmtMoney(inv.total_amount)"></span>
              <span @click="selectInvoice(inv)">
                <span :class="statusClass(inv.status)" class="px-2 py-0.5 rounded-full text-xs font-medium" x-text="inv.status || '—'"></span>
              </span>
            </div>
          </template>
        </div>
        <div class="px-4 py-2 border-t border-gray-100 text-xs text-gray-400 bg-gray-50">
          <span x-text="`Showing ${filteredInvoices.length} of ${invoices.length} invoices`"></span>
        </div>
      </div>

      <!-- Detail panel -->
      <div class="w-80 bg-white border border-gray-200 rounded-lg overflow-y-auto flex-shrink-0">
        <template x-if="!activeInvoice">
          <div class="flex items-center justify-center h-full text-gray-300 text-sm">Select an invoice</div>
        </template>
        <template x-if="activeInvoice">
          <div class="p-5">
            <div class="flex items-center gap-2 mb-3">
              <h2 class="font-bold text-slate-800 text-base" x-text="activeInvoice.invoice_number || activeInvoice.transaction_id"></h2>
              <span :class="statusClass(activeInvoice.status)" class="px-2 py-0.5 rounded-full text-xs font-medium" x-text="activeInvoice.status"></span>
            </div>
            <dl class="grid grid-cols-2 gap-x-4 gap-y-3 text-sm mb-4">
              <div><dt class="text-xs text-gray-400">PO #</dt><dd class="font-medium mt-0.5" x-text="activeInvoice.po_number || '—'"></dd></div>
              <div><dt class="text-xs text-gray-400">Trading Partner</dt><dd class="font-medium mt-0.5" x-text="activeInvoice.trading_partner || '—'"></dd></div>
              <div><dt class="text-xs text-gray-400">Invoice Date</dt><dd class="font-medium mt-0.5" x-text="activeInvoice.invoice_date || '—'"></dd></div>
              <div><dt class="text-xs text-gray-400">Due Date</dt><dd class="font-medium mt-0.5" x-text="activeInvoice.due_date || '—'"></dd></div>
              <div><dt class="text-xs text-gray-400">Subtotal</dt><dd class="font-medium mt-0.5" x-text="fmtMoney(activeInvoice.subtotal)"></dd></div>
              <div><dt class="text-xs text-gray-400">Tax</dt><dd class="font-medium mt-0.5" x-text="fmtMoney(activeInvoice.tax_amount)"></dd></div>
              <div class="col-span-2 border-t pt-3">
                <dt class="text-xs text-gray-400">Total</dt>
                <dd class="text-xl font-bold text-slate-800 mt-0.5" x-text="fmtMoney(activeInvoice.total_amount)"></dd>
              </div>
              <div class="col-span-2"><dt class="text-xs text-gray-400">Transaction ID</dt><dd class="font-mono text-xs text-gray-500 mt-0.5" x-text="activeInvoice.transaction_id"></dd></div>
            </dl>
            <!-- Line items -->
            <template x-if="activeInvoice.invoice_lines && activeInvoice.invoice_lines.length > 0">
              <div>
                <h3 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2">Line Items</h3>
                <div class="bg-gray-50 rounded-lg overflow-hidden">
                  <div class="grid text-xs text-gray-400 px-3 py-1.5 border-b border-gray-200" style="grid-template-columns: 2fr 1fr 1fr">
                    <span>Item</span><span>Qty</span><span class="text-right">Amount</span>
                  </div>
                  <template x-for="(line, i) in activeInvoice.invoice_lines" :key="i">
                    <div class="grid text-xs px-3 py-1.5 border-b border-gray-100 last:border-0" style="grid-template-columns: 2fr 1fr 1fr">
                      <span class="truncate" x-text="line.description || line.item || '—'"></span>
                      <span x-text="line.quantity || '—'"></span>
                      <span class="text-right" x-text="fmtMoney(line.line_amount || line.amount || 0)"></span>
                    </div>
                  </template>
                </div>
              </div>
            </template>
          </div>
        </template>
      </div>
    </div>

    <!-- Footer actions -->
    <div class="flex gap-3 mt-4">
      <button @click="exportCsv()" :disabled="exporting || invoices.length === 0"
              class="bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium px-4 py-2 rounded-lg disabled:opacity-50 transition">
        <span x-show="!exporting">↓ Export All &amp; Download CSV</span>
        <span x-show="exporting">Exporting…</span>
      </button>
      <button @click="syncNow()" :disabled="syncing"
              class="bg-white border border-gray-200 hover:border-gray-300 text-gray-600 text-sm px-4 py-2 rounded-lg disabled:opacity-50 transition">
        <span x-show="!syncing">↻ Refresh</span>
        <span x-show="syncing">Refreshing…</span>
      </button>
      <button disabled class="bg-white border border-dashed border-gray-300 text-gray-300 text-sm px-4 py-2 rounded-lg cursor-not-allowed">
        ⬡ Push All to NetSuite
        <span class="ml-1 bg-gray-100 text-gray-400 text-xs px-1.5 py-0.5 rounded">coming soon</span>
      </button>
    </div>

  </div>

<script>
function dashboard() {
  return {
    invoices: [],
    activeInvoice: null,
    selected: new Set(),
    activeFilter: 'All',
    search: '',
    loading: true,
    syncing: false,
    exporting: false,
    toast: '',
    lastSynced: null,
    lastExport: null,
    syncStatus: 'never',

    async init() {
      await this.loadInvoices();
      setInterval(() => this.loadInvoices(), 60000);
    },

    async loadInvoices() {
      try {
        const res = await fetch('/api/invoices');
        const data = await res.json();
        this.invoices = data.invoices || [];
        this.lastSynced = data.last_synced;
        this.syncStatus = data.status === 'ok' ? 'ok' : 'error';
      } catch {
        this.syncStatus = 'error';
      } finally {
        this.loading = false;
      }
    },

    async syncNow() {
      this.syncing = true;
      try {
        await fetch('/api/sync', { method: 'POST' });
        await this.loadInvoices();
        this.showToast('Invoices refreshed');
      } finally {
        this.syncing = false;
      }
    },

    async exportCsv() {
      this.exporting = true;
      try {
        const body = this.selected.size > 0 ? { ids: [...this.selected] } : {};
        const res = await fetch('/api/export', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!res.ok) {
          this.showToast('Export failed — try refreshing first');
          return;
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `invoices_${new Date().toISOString().slice(0,10)}.csv`;
        a.click();
        URL.revokeObjectURL(url);
        this.lastExport = new Date().toISOString();
        this.showToast('CSV downloaded');
      } finally {
        this.exporting = false;
      }
    },

    selectInvoice(inv) {
      this.activeInvoice = inv;
    },

    toggleSelect(id) {
      const next = new Set(this.selected);
      next.has(id) ? next.delete(id) : next.add(id);
      this.selected = next;
    },

    toggleAll(event) {
      if (event.target.checked) {
        this.selected = new Set(this.filteredInvoices.map(i => i.transaction_id));
      } else {
        this.selected = new Set();
      }
    },

    get filteredInvoices() {
      return this.invoices.filter(inv => {
        const matchFilter = this.activeFilter === 'All' || inv.status === this.activeFilter;
        const q = this.search.toLowerCase();
        const matchSearch = !q ||
          (inv.invoice_number || '').toLowerCase().includes(q) ||
          (inv.po_number || '').toLowerCase().includes(q);
        return matchFilter && matchSearch;
      });
    },

    get counts() {
      return {
        Draft: this.invoices.filter(i => i.status === 'Draft' || i.status === 'Open').length,
        Sent:  this.invoices.filter(i => i.status === 'Sent' || i.status === 'Completed').length,
      };
    },

    get totalValue() {
      return this.invoices.reduce((sum, i) => sum + (i.total_amount || 0), 0);
    },

    statusClass(status) {
      const s = (status || '').toLowerCase();
      if (s === 'draft' || s === 'open') return 'bg-amber-100 text-amber-800';
      if (s === 'sent' || s === 'completed') return 'bg-green-100 text-green-800';
      return 'bg-gray-100 text-gray-600';
    },

    fmtMoney(val) {
      if (!val && val !== 0) return '—';
      return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(val);
    },

    fmtTime(iso) {
      if (!iso) return '—';
      return new Date(iso).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    },

    showToast(msg) {
      this.toast = msg;
      setTimeout(() => this.toast = '', 3000);
    },
  };
}
</script>
</body>
</html>
```

- [ ] **Step 2: Verify app starts cleanly**

```bash
uvicorn app.main:app --reload --port 8000
```

Expected: server starts, no errors. Visit `http://localhost:8000` — dashboard loads.

- [ ] **Step 3: Run full test suite**

```bash
pytest -v
```

Expected: all tests PASSED.

- [ ] **Step 4: Commit**

```bash
git add app/static/index.html
git commit -m "feat: add invoice dashboard UI (Tailwind + Alpine.js, master/detail split)"
```

---

## Task 6: Docker

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`

- [ ] **Step 1: Create Dockerfile**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Create docker-compose.yml**

```yaml
services:
  dashboard:
    build: .
    ports:
      - "8000:8000"
    env_file: .env
    restart: unless-stopped
    volumes:
      - .tmp:/app/.tmp
```

The `.tmp` volume persists the token cache across container restarts so the app doesn't re-authenticate on every restart.

- [ ] **Step 3: Build and verify**

```bash
docker compose build
docker compose up -d
```

Expected: container starts. Visit `http://localhost:8000` — dashboard loads.

```bash
docker compose logs -f
```

Expected: `Application startup complete.` with no errors.

- [ ] **Step 4: Verify .env is not baked into the image**

```bash
docker compose run --rm dashboard env | grep CRSTL
```

Expected: values come from your local `.env` file, not the image.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile docker-compose.yml
git commit -m "feat: add Dockerfile and docker-compose for Proxmox CT deploy"
```

---

## Self-Review

**Spec coverage check:**
- ✅ FastAPI backend with 4 endpoints
- ✅ In-memory cache with `last_synced` timestamp
- ✅ APScheduler daily 7am refresh
- ✅ Export accepts optional `ids` list for subset export
- ✅ NetSuite stub returns 501
- ✅ Stat cards: Draft, Sent, Total Value, Last Export
- ✅ Search filters on invoice # and PO #
- ✅ Filter pills: All / Draft / Sent — stat card click sets filter
- ✅ Multi-select with contextual action bar
- ✅ Master/detail split — detail panel always visible
- ✅ Detail shows: invoice #, status, PO #, due date, subtotal, tax, total, trading partner, transaction ID, line items
- ✅ Empty state on detail panel
- ✅ Export spinner + toast notification
- ✅ Auto-refresh every 60s
- ✅ Error state: sync failed → status dot turns red
- ✅ Export with empty cache → 503
- ✅ Docker + docker-compose
- ✅ `.tmp` volume for token cache persistence
