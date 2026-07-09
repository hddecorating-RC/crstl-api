# Export Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add SQLite-backed per-invoice export tracking so the dashboard shows "last exported" badges on each row and in the detail panel.

**Architecture:** A new `app/tracking.py` module owns a SQLite database at `.tmp/tracking.db`. `POST /api/export` calls `record_events()` after generating the CSV. `GET /api/invoices` calls `get_latest_events()` and merges `exported_at` / `netsuite_at` fields into each invoice before returning. The frontend reads these fields and renders small badges inline.

**Tech Stack:** Python `sqlite3` (stdlib), FastAPI, Alpine.js, Tailwind CDN

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `app/tracking.py` | Create | SQLite init, `record_events`, `get_latest_events` |
| `app/main.py` | Modify | Call tracking in `export` route; merge tracking into `get_invoices` |
| `app/static/index.html` | Modify | Render `exported_at` badge in list row and detail panel |
| `tests/test_tracking.py` | Create | Unit tests for tracking module |
| `tests/test_api.py` | Modify | Add test: export sets `exported_at` on subsequent `GET /api/invoices` |

---

## Task 1: Tracking Module

**Files:**
- Create: `app/tracking.py`
- Create: `tests/test_tracking.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_tracking.py`:

```python
import os
import tempfile
import pytest
from app.tracking import record_events, get_latest_events, init_db


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / ".tmp" / "tracking.db")
    monkeypatch.setenv("TRACKING_DB", path)
    init_db()
    return path


def test_get_latest_events_empty(db_path):
    result = get_latest_events(["tx-001", "tx-002"])
    assert result == {
        "tx-001": {"exported_at": None, "netsuite_at": None},
        "tx-002": {"exported_at": None, "netsuite_at": None},
    }


def test_record_and_get_exported(db_path):
    record_events(["tx-001", "tx-002"], "exported")
    result = get_latest_events(["tx-001", "tx-002", "tx-003"])
    assert result["tx-001"]["exported_at"] is not None
    assert result["tx-002"]["exported_at"] is not None
    assert result["tx-003"]["exported_at"] is None
    assert result["tx-001"]["netsuite_at"] is None


def test_record_updates_to_most_recent(db_path):
    record_events(["tx-001"], "exported")
    first = get_latest_events(["tx-001"])["tx-001"]["exported_at"]
    record_events(["tx-001"], "exported")
    second = get_latest_events(["tx-001"])["tx-001"]["exported_at"]
    assert second >= first


def test_record_netsuite_event(db_path):
    record_events(["tx-001"], "netsuite")
    result = get_latest_events(["tx-001"])
    assert result["tx-001"]["netsuite_at"] is not None
    assert result["tx-001"]["exported_at"] is None


def test_empty_ids_list(db_path):
    record_events([], "exported")  # should not raise
    result = get_latest_events([])
    assert result == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/ritchiecheung/Work/crstl-api && python3 -m pytest tests/test_tracking.py -v
```

Expected: `ImportError: cannot import name 'record_events' from 'app.tracking'`

- [ ] **Step 3: Create `app/tracking.py`**

```python
import os
import pathlib
import sqlite3
from datetime import datetime, timezone


def _db_path() -> str:
    return os.environ.get("TRACKING_DB", ".tmp/tracking.db")


def init_db() -> None:
    path = _db_path()
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS invoice_events (
                transaction_id TEXT NOT NULL,
                event_type     TEXT NOT NULL CHECK(event_type IN ('exported', 'netsuite')),
                occurred_at    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_invoice_events_tx
                ON invoice_events(transaction_id);
            PRAGMA journal_mode=WAL;
        """)


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(_db_path(), check_same_thread=False)


def record_events(transaction_ids: list[str], event_type: str) -> None:
    if not transaction_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    rows = [(tx_id, event_type, now) for tx_id in transaction_ids]
    try:
        with _connect() as conn:
            conn.executemany(
                "INSERT INTO invoice_events (transaction_id, event_type, occurred_at) VALUES (?, ?, ?)",
                rows,
            )
    except Exception as exc:
        print(f"WARNING: tracking write failed: {exc}")


def get_latest_events(transaction_ids: list[str]) -> dict[str, dict]:
    if not transaction_ids:
        return {}
    result = {tx_id: {"exported_at": None, "netsuite_at": None} for tx_id in transaction_ids}
    placeholders = ",".join("?" * len(transaction_ids))
    try:
        with _connect() as conn:
            rows = conn.execute(
                f"""
                SELECT transaction_id, event_type, MAX(occurred_at)
                FROM invoice_events
                WHERE transaction_id IN ({placeholders})
                GROUP BY transaction_id, event_type
                """,
                transaction_ids,
            ).fetchall()
        for tx_id, event_type, occurred_at in rows:
            if event_type == "exported":
                result[tx_id]["exported_at"] = occurred_at
            elif event_type == "netsuite":
                result[tx_id]["netsuite_at"] = occurred_at
    except Exception as exc:
        print(f"WARNING: tracking read failed: {exc}")
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_tracking.py -v
```

Expected: 5 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/tracking.py tests/test_tracking.py
git commit -m "feat: add SQLite export tracking module"
```

---

## Task 2: Wire Tracking into API

**Files:**
- Modify: `app/main.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_api.py` (append after existing tests):

```python
def test_export_sets_exported_at(client, monkeypatch, tmp_path):
    import app.tracking as tracking
    monkeypatch.setenv("TRACKING_DB", str(tmp_path / "tracking.db"))
    tracking.init_db()

    client.post("/api/sync")
    client.post("/api/export", json={})

    resp = client.get("/api/invoices")
    invoices = resp.json()["invoices"]
    assert len(invoices) > 0
    assert invoices[0]["exported_at"] is not None
    assert invoices[0]["netsuite_at"] is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_api.py::test_export_sets_exported_at -v
```

Expected: FAIL — `exported_at` key missing from invoice dict.

- [ ] **Step 3: Update `app/main.py`**

Add the import at the top of the file (after `from app.export import build_csv`):

```python
from app import tracking
```

Add `init_db()` call inside the `lifespan` context manager, before `yield`:

```python
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    tracking.init_db()
    await asyncio.to_thread(_refresh_cache)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(_refresh_cache, "cron", hour=7, minute=0)
    scheduler.start()
    yield
    scheduler.shutdown()
```

Replace the `get_invoices` route:

```python
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
```

Replace the `export` route:

```python
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

    exported_ids = body.ids if body.ids is not None else [inv["transaction_id"] for inv in invoices]
    tracking.record_events(exported_ids, "exported")

    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
```

- [ ] **Step 4: Run all tests to verify they pass**

```bash
python3 -m pytest -v
```

Expected: 15 tests PASSED (14 existing + 1 new).

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_api.py
git commit -m "feat: wire export tracking into GET /api/invoices and POST /api/export"
```

---

## Task 3: Frontend Badges

**Files:**
- Modify: `app/static/index.html`

- [ ] **Step 1: Add export badge to master list row**

In `app/static/index.html`, the master list row currently has this grid (line ~116):

```html
<div class="grid items-center px-4 py-2.5 border-b border-gray-50 cursor-pointer hover:bg-gray-50 text-sm transition"
     style="grid-template-columns: 32px 1fr 1fr 100px 80px 90px"
```

Change the grid template and add a badge column after Status:

```html
<div class="grid items-center px-4 py-2.5 border-b border-gray-50 cursor-pointer hover:bg-gray-50 text-sm transition"
     style="grid-template-columns: 32px 1fr 1fr 100px 80px 90px 70px"
```

Also update the table header (line ~97) to match:

```html
<div class="grid text-xs font-semibold text-gray-400 uppercase tracking-wide px-4 py-2 border-b border-gray-100 bg-gray-50"
     style="grid-template-columns: 32px 1fr 1fr 100px 80px 90px 70px">
  <span><input type="checkbox" @change="toggleAll($event)" class="w-3.5 h-3.5"></span>
  <span>Invoice #</span>
  <span>PO #</span>
  <span>Date</span>
  <span>Total</span>
  <span>Status</span>
  <span>Exported</span>
</div>
```

Add the badge cell at the end of the row template (after the Status `<span>`):

```html
<span @click="selectInvoice(inv)">
  <span x-show="inv.exported_at"
        class="px-1.5 py-0.5 rounded text-xs bg-gray-100 text-gray-500 font-medium"
        x-text="inv.exported_at ? '↓ ' + fmtDate(inv.exported_at) : ''"></span>
</span>
```

- [ ] **Step 2: Add export history to detail panel**

In the detail panel, after the Transaction ID `<div>` (line ~158):

```html
<div class="col-span-2"><dt class="text-xs text-gray-400">Transaction ID</dt><dd class="font-mono text-xs text-gray-500 mt-0.5" x-text="activeInvoice.transaction_id"></dd></div>
```

Add immediately after it (still inside the `<dl>`):

```html
<div class="col-span-2 border-t pt-3">
  <dt class="text-xs text-gray-400 mb-1">Export History</dt>
  <dd class="text-xs text-gray-600">
    <span class="block">
      CSV: <span x-text="activeInvoice.exported_at ? fmtTime(activeInvoice.exported_at) : '—'"></span>
    </span>
    <span class="block mt-0.5">
      NetSuite: <span x-text="activeInvoice.netsuite_at ? fmtTime(activeInvoice.netsuite_at) : '—'"></span>
    </span>
  </dd>
</div>
```

- [ ] **Step 3: Add `fmtDate` helper to the Alpine.js component**

In the `dashboard()` function, add `fmtDate` alongside `fmtTime` (around line ~330):

```js
fmtDate(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleString('en-US', { month: 'short', day: 'numeric' });
},
```

- [ ] **Step 4: Run full test suite**

```bash
python3 -m pytest -v
```

Expected: 15 tests PASSED. (Frontend changes don't affect Python tests.)

- [ ] **Step 5: Commit**

```bash
git add app/static/index.html
git commit -m "feat: add exported_at badge to invoice list row and detail panel"
```

---

## Task 4: Push to GitHub

- [ ] **Step 1: Push all commits**

```bash
git push
```

Expected: 3 commits pushed to `origin/main`.

- [ ] **Step 2: Smoke test with mock data**

Ensure `.env` has `MOCK_DATA=true`, then:

```bash
uvicorn app.main:app --reload --port 8000
```

1. Open `http://localhost:8000`
2. Click "Export All & Download CSV" — CSV downloads
3. Click "↻ Refresh" to reload the invoice list
4. Verify each row now shows a gray "↓ Jul 9" badge in the Exported column
5. Click an invoice — detail panel shows "CSV: Jul 9, 2:14pm" under Export History
