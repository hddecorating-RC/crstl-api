# NetSuite CSV Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate a daily NetSuite-formatted CSV export (scheduled at 4AM EST) by pulling 810 invoices from the cache and resolving ship-to province from Crstl 850 POs, downloadable from the dashboard.

**Architecture:** A shared transformer (`app/netsuite.py`) converts each invoice + province into a NetSuite record shape; `app/netsuite_csv.py` renders those shapes as a CSV importable by NetSuite's invoice importer. Province is resolved by fetching 850 POs from Crstl and joining on `po_number`. APScheduler runs the export daily at 4AM EST. The dashboard exposes generate-on-demand and download endpoints. `app/netsuite_client.py` is a stub that documents the future REST API interface.

**Tech Stack:** Python sqlite3, FastAPI, APScheduler, Alpine.js, Tailwind CDN

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `config/netsuite_customers.json` | Create | Province/store → NetSuite customer ID + tax code mapping |
| `app/netsuite.py` | Create | Shared transformer: invoice dict + province → NetSuite record shape |
| `app/netsuite_csv.py` | Create | Renders NetSuite record shapes as CSV bytes |
| `app/netsuite_client.py` | Create | REST API stub (documents future interface, raises NotImplementedError) |
| `app/crstl.py` | Modify | Add `fetch_po_provinces()` method |
| `app/main.py` | Modify | Add netsuite state, `_generate_netsuite_export()`, scheduler job, 3 new endpoints |
| `app/static/index.html` | Modify | NetSuite export status card + download/generate buttons |
| `tests/test_netsuite.py` | Create | Unit tests for transformer and CSV renderer |
| `tests/test_api.py` | Modify | Tests for 3 new NetSuite export API endpoints |

---

## Task 1: Config File + Transformer

**Files:**
- Create: `config/netsuite_customers.json`
- Create: `app/netsuite.py`
- Create: `tests/test_netsuite.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_netsuite.py`:

```python
import json
import pytest
from unittest.mock import patch

SAMPLE_INVOICE = {
    "transaction_id": "tx-001",
    "invoice_number": "INV-001",
    "po_number": "PO-12345",
    "invoice_date": "2026-07-07",
    "due_date": "2026-08-06",
    "subtotal": 18000.00,
    "tax_amount": 2340.00,
    "total_amount": 20340.00,
}

SAMPLE_CONFIG = {
    "wholesale_stores": {
        "VAUGHAN": {"customer_id": "cust-vaughan", "province": "ON", "tax_code": "HST-ON", "tax_rate": 0.13},
        "CALGARY": {"customer_id": "cust-calgary", "province": "AB", "tax_code": "GST", "tax_rate": 0.05},
    },
    "dropship_provinces": {
        "QC": {"customer_id": "cust-qc", "tax_code": "GST+QST", "tax_rate": 0.14975},
        "ON": {"customer_id": "cust-on-ds", "tax_code": "HST-ON", "tax_rate": 0.13},
    },
    "item": "Merchandise Sales",
    "currency": "CAD",
}


@pytest.fixture
def mock_config():
    with patch("app.netsuite._load_config", return_value=SAMPLE_CONFIG):
        yield


def test_transform_wholesale_vaughan(mock_config):
    from app.netsuite import transform_invoice
    result = transform_invoice(SAMPLE_INVOICE, province="ON", store="VAUGHAN")
    assert result is not None
    assert result["external_id"] == "PO-12345"
    assert result["customer_id"] == "cust-vaughan"
    assert result["tax_code"] == "HST-ON"
    assert result["rate"] == 18000.00
    assert result["tax_amount"] == 2340.00
    assert result["tran_date"] == "2026-07-07"
    assert result["due_date"] == "2026-08-06"
    assert result["memo"] == "PO-12345"
    assert result["other_ref_num"] == "PO-12345"
    assert result["currency"] == "CAD"
    assert result["item"] == "Merchandise Sales"
    assert result["quantity"] == 1


def test_transform_dropship_quebec(mock_config):
    from app.netsuite import transform_invoice
    result = transform_invoice(SAMPLE_INVOICE, province="QC", store=None)
    assert result is not None
    assert result["customer_id"] == "cust-qc"
    assert result["tax_code"] == "GST+QST"


def test_transform_unknown_province_returns_none(mock_config):
    from app.netsuite import transform_invoice
    result = transform_invoice(SAMPLE_INVOICE, province="XX", store=None)
    assert result is None


def test_transform_none_province_no_store_returns_none(mock_config):
    from app.netsuite import transform_invoice
    result = transform_invoice(SAMPLE_INVOICE, province=None, store=None)
    assert result is None


def test_transform_unknown_store_returns_none(mock_config):
    from app.netsuite import transform_invoice
    result = transform_invoice(SAMPLE_INVOICE, province="ON", store="TORONTO")
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/ritchiecheung/Work/crstl-api && python3 -m pytest tests/test_netsuite.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.netsuite'`

- [ ] **Step 3: Create `config/netsuite_customers.json`**

```bash
mkdir -p /Users/ritchiecheung/Work/crstl-api/config
```

Write `/Users/ritchiecheung/Work/crstl-api/config/netsuite_customers.json`:

```json
{
  "wholesale_stores": {
    "VAUGHAN": {
      "customer_id": "PLACEHOLDER_VAUGHAN",
      "province": "ON",
      "tax_code": "HST-ON",
      "tax_rate": 0.13
    },
    "CALGARY": {
      "customer_id": "PLACEHOLDER_CALGARY",
      "province": "AB",
      "tax_code": "GST",
      "tax_rate": 0.05
    }
  },
  "dropship_provinces": {
    "AB": {"customer_id": "PLACEHOLDER_DS_AB", "tax_code": "GST", "tax_rate": 0.05},
    "BC": {"customer_id": "PLACEHOLDER_DS_BC", "tax_code": "GST+PST", "tax_rate": 0.12},
    "MB": {"customer_id": "PLACEHOLDER_DS_MB", "tax_code": "GST+PST", "tax_rate": 0.12},
    "NB": {"customer_id": "PLACEHOLDER_DS_NB", "tax_code": "HST", "tax_rate": 0.15},
    "NL": {"customer_id": "PLACEHOLDER_DS_NL", "tax_code": "HST", "tax_rate": 0.15},
    "NT": {"customer_id": "PLACEHOLDER_DS_NT", "tax_code": "GST", "tax_rate": 0.05},
    "NS": {"customer_id": "PLACEHOLDER_DS_NS", "tax_code": "HST", "tax_rate": 0.14},
    "NU": {"customer_id": "PLACEHOLDER_DS_NU", "tax_code": "GST", "tax_rate": 0.05},
    "ON": {"customer_id": "PLACEHOLDER_DS_ON", "tax_code": "HST-ON", "tax_rate": 0.13},
    "PE": {"customer_id": "PLACEHOLDER_DS_PE", "tax_code": "HST", "tax_rate": 0.15},
    "QC": {"customer_id": "PLACEHOLDER_DS_QC", "tax_code": "GST+QST", "tax_rate": 0.14975},
    "SK": {"customer_id": "PLACEHOLDER_DS_SK", "tax_code": "GST+PST", "tax_rate": 0.11},
    "YT": {"customer_id": "PLACEHOLDER_DS_YT", "tax_code": "GST", "tax_rate": 0.05}
  },
  "item": "Merchandise Sales",
  "currency": "CAD"
}
```

- [ ] **Step 4: Create `app/netsuite.py`**

```python
import json
import pathlib


def _load_config() -> dict:
    return json.loads(pathlib.Path("config/netsuite_customers.json").read_text())


def transform_invoice(invoice: dict, province: str | None, store: str | None) -> dict | None:
    """
    Transform a Crstl invoice dict into a NetSuite record shape.
    Returns None if the province/store has no mapping in config.

    store: uppercase city key for wholesale ("VAUGHAN", "CALGARY"), or None for dropship.
    province: 2-letter Canadian province code ("ON", "QC", etc.), used when store is None.
    """
    config = _load_config()

    if store:
        mapping = config["wholesale_stores"].get(store.upper())
    elif province:
        mapping = config["dropship_provinces"].get(province.upper())
    else:
        return None

    if not mapping:
        return None

    return {
        "external_id":   invoice["po_number"],
        "customer_id":   mapping["customer_id"],
        "tran_date":     invoice["invoice_date"],
        "due_date":      invoice["due_date"],
        "memo":          invoice["po_number"],
        "other_ref_num": invoice["po_number"],
        "currency":      config["currency"],
        "item":          config["item"],
        "quantity":      1,
        "rate":          invoice["subtotal"],
        "amount":        invoice["subtotal"],
        "tax_code":      mapping["tax_code"],
        "tax_amount":    invoice["tax_amount"],
        "total_amount":  invoice["total_amount"],
    }
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_netsuite.py -v
```

Expected: 5 tests PASSED.

- [ ] **Step 6: Commit**

```bash
git add config/netsuite_customers.json app/netsuite.py tests/test_netsuite.py
git commit -m "feat: add NetSuite config and invoice transformer"
```

---

## Task 2: CSV Renderer

**Files:**
- Create: `app/netsuite_csv.py`
- Modify: `tests/test_netsuite.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_netsuite.py`:

```python
SAMPLE_RECORD = {
    "external_id":   "PO-12345",
    "customer_id":   "cust-vaughan",
    "tran_date":     "2026-07-07",
    "due_date":      "2026-08-06",
    "memo":          "PO-12345",
    "other_ref_num": "PO-12345",
    "item":          "Merchandise Sales",
    "quantity":      1,
    "rate":          18000.00,
    "amount":        18000.00,
    "tax_code":      "HST-ON",
    "tax_amount":    2340.00,
    "total_amount":  20340.00,
    "currency":      "CAD",
}


def test_netsuite_csv_has_correct_headers():
    from app.netsuite_csv import build_netsuite_csv, COLUMNS
    csv_bytes = build_netsuite_csv([])
    header = csv_bytes.decode().splitlines()[0]
    for col in COLUMNS:
        assert col in header


def test_netsuite_csv_one_row_per_record():
    from app.netsuite_csv import build_netsuite_csv
    csv_bytes = build_netsuite_csv([SAMPLE_RECORD, SAMPLE_RECORD])
    lines = csv_bytes.decode().splitlines()
    assert len(lines) == 3  # header + 2 rows


def test_netsuite_csv_row_values():
    from app.netsuite_csv import build_netsuite_csv
    csv_bytes = build_netsuite_csv([SAMPLE_RECORD])
    lines = csv_bytes.decode().splitlines()
    assert len(lines) == 2
    row = lines[1]
    assert "PO-12345" in row
    assert "cust-vaughan" in row
    assert "18000.0" in row
    assert "HST-ON" in row
    assert "CAD" in row
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_netsuite.py::test_netsuite_csv_has_correct_headers -v
```

Expected: `ModuleNotFoundError: No module named 'app.netsuite_csv'`

- [ ] **Step 3: Create `app/netsuite_csv.py`**

```python
import csv
import io

COLUMNS = [
    "External ID",
    "Customer",
    "Date",
    "Due Date",
    "Memo",
    "PO Number",
    "Item",
    "Quantity",
    "Rate",
    "Tax Code",
    "Tax Amount",
    "Currency",
]


def build_netsuite_csv(records: list[dict]) -> bytes:
    """Render NetSuite invoice import CSV from transformed records."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS)
    writer.writeheader()
    for r in records:
        writer.writerow({
            "External ID": r["external_id"],
            "Customer":    r["customer_id"],
            "Date":        r["tran_date"],
            "Due Date":    r["due_date"],
            "Memo":        r["memo"],
            "PO Number":   r["other_ref_num"],
            "Item":        r["item"],
            "Quantity":    r["quantity"],
            "Rate":        r["rate"],
            "Tax Code":    r["tax_code"],
            "Tax Amount":  r["tax_amount"],
            "Currency":    r["currency"],
        })
    return buf.getvalue().encode("utf-8")
```

- [ ] **Step 4: Run all netsuite tests**

```bash
python3 -m pytest tests/test_netsuite.py -v
```

Expected: 8 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/netsuite_csv.py tests/test_netsuite.py
git commit -m "feat: add NetSuite CSV renderer"
```

---

## Task 3: 850 Province Lookup

**Files:**
- Modify: `app/crstl.py`
- No new test file — add to `tests/test_netsuite.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_netsuite.py`:

```python
def test_fetch_po_provinces_extracts_store_and_province():
    from unittest.mock import MagicMock, patch
    from app.crstl import CrstlClient

    client = CrstlClient(
        base_url="https://api.crstl.ai/v2",
        email="test@test.com",
        password="pw",
    )

    mock_transactions = [
        {"id": "850-001", "reference_id": "PO-40850625"},
        {"id": "850-002", "reference_id": "PO-537608514"},
    ]
    mock_detail_vaughan = {
        "transaction_data": {
            "ship_to_party_state": "ON",
            "ship_to_party_name": "VAUGHAN STOCK AND FLOW - 7275",
        }
    }
    mock_detail_dropship = {
        "transaction_data": {
            "ship_to_party_state": "QC",
            "ship_to_party_name": "GELINAS ANICK",
        }
    }

    with patch.object(client, "get_access_token", return_value="tok"), \
         patch.object(client, "_fetch_all_transactions", return_value=mock_transactions), \
         patch.object(client, "_fetch_transaction_detail", side_effect=[mock_detail_vaughan, mock_detail_dropship]):
        result = client.fetch_po_provinces()

    assert result["PO-40850625"]["province"] == "ON"
    assert result["PO-40850625"]["store"] == "VAUGHAN"
    assert result["PO-537608514"]["province"] == "QC"
    assert result["PO-537608514"]["store"] is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_netsuite.py::test_fetch_po_provinces_extracts_store_and_province -v
```

Expected: `AttributeError: 'CrstlClient' object has no attribute 'fetch_po_provinces'`

- [ ] **Step 3: Add `fetch_po_provinces` to `app/crstl.py`**

Add this method to the `CrstlClient` class, after `fetch_invoices`:

```python
def fetch_po_provinces(self) -> dict[str, dict]:
    """
    Fetch all 850 POs and return {po_number: {"province": "ON", "store": "VAUGHAN"|None}}.
    store is set for wholesale orders (identified by ship-to name containing VAUGHAN or CALGARY).
    province is the 2-letter Canadian province code from the ship-to address.
    """
    token = self.get_access_token()
    pos = self._fetch_all_transactions(token, transaction_type="850")
    result = {}
    for po in pos:
        po_id = po.get("id") or po.get("transaction_id")
        po_number = str(po.get("reference_id") or "")
        if not po_id or not po_number:
            continue
        try:
            detail = self._fetch_transaction_detail(po_id, token)
            tx_data = detail.get("transaction_data") or detail.get("document_data") or {}

            province = str(
                tx_data.get("ship_to_party_state") or
                tx_data.get("ship_to_state") or
                detail.get("ship_to_state") or ""
            ).upper().strip()

            ship_to_name = str(
                tx_data.get("ship_to_party_name") or
                tx_data.get("ship_to_name") or
                detail.get("ship_to_name") or ""
            ).upper()

            if not province:
                continue

            store = None
            if "VAUGHAN" in ship_to_name:
                store = "VAUGHAN"
            elif "CALGARY" in ship_to_name:
                store = "CALGARY"

            result[po_number] = {"province": province, "store": store}
        except Exception as exc:
            print(f"WARNING: failed to fetch 850 detail for {po_number}: {exc}")
    return result
```

- [ ] **Step 4: Run all tests**

```bash
python3 -m pytest -v
```

Expected: all existing tests + new test PASSED (21 total).

- [ ] **Step 5: Commit**

```bash
git add app/crstl.py tests/test_netsuite.py
git commit -m "feat: add fetch_po_provinces to CrstlClient for 850 ship-to province lookup"
```

---

## Task 4: Scheduler + API Endpoints

**Files:**
- Create: `app/netsuite_client.py`
- Modify: `app/main.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write failing tests**

Read `tests/test_api.py` first. Append to it:

```python
def test_netsuite_export_latest_initially_unavailable(client):
    resp = client.get("/api/netsuite-export/latest")
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is False
    assert data["last_generated"] is None
    assert data["count"] == 0


def test_netsuite_export_download_no_file_returns_404(client):
    resp = client.get("/api/netsuite-export/download")
    assert resp.status_code == 404


def test_netsuite_generate_and_download(client, monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_DATA", "true")

    client.post("/api/sync")
    resp = client.post("/api/netsuite-export/generate")
    assert resp.status_code == 200
    assert resp.json()["available"] is True

    download = client.get("/api/netsuite-export/download")
    assert download.status_code == 200
    assert "text/csv" in download.headers["content-type"]
    assert "netsuite_export" in download.headers["content-disposition"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_api.py::test_netsuite_export_latest_initially_unavailable -v
```

Expected: `404 Not Found` — route doesn't exist yet.

- [ ] **Step 3: Create `app/netsuite_client.py`**

```python
"""
NetSuite REST API client — stub placeholder.
Replace with TBA-authenticated implementation once credentials are available.

Future interface:
    client = NetSuiteClient(account_id, consumer_key, consumer_secret, token_id, token_secret)
    client.upsert_invoice(record)   # PUT /invoice/eid:{external_id}
"""


class NetSuiteClient:
    def upsert_invoice(self, record: dict) -> dict:
        raise NotImplementedError(
            "NetSuite REST connector not yet configured. "
            "Use the CSV export path until TBA credentials are available."
        )
```

- [ ] **Step 4: Update `app/main.py`**

Read the full `app/main.py` first. Then make the following additions:

**a) Add imports** at the top (after existing imports):

```python
from app.netsuite import transform_invoice
from app.netsuite_csv import build_netsuite_csv
```

**b) Add netsuite state** after `_cache_lock = threading.Lock()`:

```python
_netsuite_state: dict = {"last_generated": None, "path": None, "count": 0, "skipped": 0}
_netsuite_lock = threading.Lock()

# Province map used in MOCK_DATA mode (cycles through stores + provinces for 50 mock invoices)
_MOCK_PO_PROVINCES: dict[str, dict] = {}
for _i in range(50):
    _po = f"PO-{98801 - _i * 3}"
    if _i % 10 < 2:
        _MOCK_PO_PROVINCES[_po] = {"province": "ON", "store": "VAUGHAN"}
    elif _i % 10 < 4:
        _MOCK_PO_PROVINCES[_po] = {"province": "AB", "store": "CALGARY"}
    else:
        _provinces = ["ON", "BC", "QC", "AB", "SK", "MB", "NS", "NB", "NL", "PE", "NT", "YT", "NU"]
        _MOCK_PO_PROVINCES[_po] = {"province": _provinces[_i % len(_provinces)], "store": None}
```

**c) Add `_generate_netsuite_export` function** after `_refresh_cache`:

```python
def _generate_netsuite_export() -> None:
    with _cache_lock:
        invoices = list(_cache["invoices"])

    if not invoices:
        print("NetSuite export: cache empty, skipping")
        return

    if os.environ.get("MOCK_DATA", "").lower() in ("1", "true", "yes"):
        po_provinces = _MOCK_PO_PROVINCES
    else:
        try:
            po_provinces = _get_client().fetch_po_provinces()
        except Exception as exc:
            print(f"WARNING: NetSuite export — failed to fetch PO provinces: {exc}")
            po_provinces = {}

    records, skipped = [], []
    for inv in invoices:
        loc = po_provinces.get(inv.get("po_number", ""), {})
        record = transform_invoice(inv, province=loc.get("province"), store=loc.get("store"))
        if record is None:
            skipped.append(inv.get("po_number", "?"))
        else:
            records.append(record)

    if skipped:
        print(f"WARNING: NetSuite export skipped {len(skipped)} invoices (no province mapping): {skipped}")

    csv_bytes = build_netsuite_csv(records)
    out_path = pathlib.Path(".tmp") / f"netsuite_export_{date.today().isoformat()}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(csv_bytes)

    with _netsuite_lock:
        _netsuite_state["last_generated"] = datetime.now(timezone.utc).isoformat()
        _netsuite_state["path"] = str(out_path)
        _netsuite_state["count"] = len(records)
        _netsuite_state["skipped"] = len(skipped)

    print(f"NetSuite export: {len(records)} invoices → {out_path} ({len(skipped)} skipped)")
```

**d) Add the netsuite scheduler job** inside the `lifespan` context manager, after the existing `scheduler.add_job` line:

```python
scheduler.add_job(_generate_netsuite_export, "cron", hour=4, minute=0, timezone="America/Toronto")
```

**e) Add three new endpoints** before `app.mount(...)`:

```python
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
    try:
        await asyncio.to_thread(_generate_netsuite_export)
    except Exception as exc:
        return JSONResponse(status_code=500, content={"message": str(exc)})
    with _netsuite_lock:
        state = {**_netsuite_state}
    path = state.get("path")
    state["available"] = bool(path and pathlib.Path(path).exists())
    return state
```

Also replace the existing `POST /api/netsuite` stub to reference the future client:

```python
@app.post("/api/netsuite")
def netsuite() -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={"message": "NetSuite REST connector not yet configured. Use CSV export path."},
    )
```

- [ ] **Step 5: Run all tests**

```bash
python3 -m pytest -v
```

Expected: 24 tests PASSED (21 existing + 3 new).

- [ ] **Step 6: Commit**

```bash
git add app/main.py app/netsuite_client.py tests/test_api.py
git commit -m "feat: add NetSuite export scheduler, generate/download endpoints, REST stub"
```

---

## Task 5: Frontend Download Button

**Files:**
- Modify: `app/static/index.html`

- [ ] **Step 1: Add NetSuite export state to Alpine component**

Read `app/static/index.html` first.

In the `dashboard()` function, add after the existing state properties (`lastExport`, `syncStatus`, etc.):

```js
netsuiteExport: { available: false, last_generated: null, count: 0, skipped: 0 },
generatingNetsuite: false,
```

- [ ] **Step 2: Add `loadNetsuiteStatus` and `generateNetsuite` methods**

In the `dashboard()` function, add after the `syncNow` method:

```js
async loadNetsuiteStatus() {
  try {
    const res = await fetch('/api/netsuite-export/latest');
    this.netsuiteExport = await res.json();
  } catch {}
},

async generateNetsuite() {
  this.generatingNetsuite = true;
  try {
    const res = await fetch('/api/netsuite-export/generate', { method: 'POST' });
    this.netsuiteExport = await res.json();
    this.showToast('NetSuite export generated');
  } catch {
    this.showToast('NetSuite export failed');
  } finally {
    this.generatingNetsuite = false;
  }
},
```

- [ ] **Step 3: Call `loadNetsuiteStatus` in `init()`**

Find the `async init()` method and add the call after `loadInvoices()`:

```js
async init() {
  await this.loadInvoices();
  await this.loadNetsuiteStatus();
  setInterval(() => this.loadInvoices(), 60000);
},
```

- [ ] **Step 4: Add NetSuite export stat card**

Find the stat cards row (the `<div class="grid grid-cols-4 ...">` or similar). Add a 5th card after the existing "Last Export" card:

```html
<!-- NetSuite Export -->
<div class="bg-white border border-gray-200 rounded-lg px-4 py-3">
  <div class="text-xs text-gray-400 font-medium uppercase tracking-wide mb-1">NetSuite Export</div>
  <div class="text-sm font-semibold text-slate-800 mt-1"
       x-text="netsuiteExport.last_generated ? fmtTime(netsuiteExport.last_generated) : '—'"></div>
  <div class="text-xs text-gray-400 mt-1"
       x-show="netsuiteExport.last_generated"
       x-text="`${netsuiteExport.count} invoices`"></div>
  <div class="flex gap-2 mt-2">
    <button @click="generateNetsuite()" :disabled="generatingNetsuite"
            class="text-xs bg-slate-100 hover:bg-slate-200 text-slate-600 font-medium px-2 py-1 rounded disabled:opacity-50">
      <span x-show="!generatingNetsuite">⟳ Generate</span>
      <span x-show="generatingNetsuite">Generating…</span>
    </button>
    <a x-show="netsuiteExport.available"
       href="/api/netsuite-export/download"
       class="text-xs bg-green-600 hover:bg-green-700 text-white font-medium px-2 py-1 rounded">
      ↓ Download
    </a>
  </div>
</div>
```

Also update the stat cards grid class from `grid-cols-4` to `grid-cols-5` (or whichever number matches the current card count).

- [ ] **Step 5: Run all tests**

```bash
python3 -m pytest -v
```

Expected: 24 tests PASSED. (Frontend changes don't affect Python tests.)

- [ ] **Step 6: Commit**

```bash
git add app/static/index.html
git commit -m "feat: add NetSuite export status card with generate and download buttons"
```

---

## Task 6: Push to GitHub

- [ ] **Step 1: Push all commits**

```bash
git push
```

Expected: 5 commits pushed to `origin/main`.

- [ ] **Step 2: Smoke test with mock data**

Ensure `.env` has `MOCK_DATA=true`, then:

```bash
uvicorn app.main:app --reload --port 8000
```

1. Open `http://localhost:8000`
2. Click "⟳ Generate" in the NetSuite Export card
3. Verify count shows (e.g. "42 invoices")
4. Click "↓ Download" — verify CSV downloads named `netsuite_export_YYYY-MM-DD.csv`
5. Open the CSV — verify columns: `External ID`, `Customer`, `Date`, `Due Date`, `Memo`, `PO Number`, `Item`, `Quantity`, `Rate`, `Tax Code`, `Tax Amount`, `Currency`
6. Verify wholesale invoices have `PLACEHOLDER_VAUGHAN` or `PLACEHOLDER_CALGARY` as Customer
7. Verify dropship invoices have province-specific customers (e.g. `PLACEHOLDER_DS_QC`)
8. Verify skipped count in console for any unresolvable PO numbers
