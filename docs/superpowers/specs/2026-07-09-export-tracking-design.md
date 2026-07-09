# Export Tracking — Design Spec
**Date:** 2026-07-09
**Status:** Approved

## Overview

Add per-invoice export tracking to the HD Decorating invoice dashboard. When an invoice is exported to CSV or pushed to NetSuite, a timestamp is recorded in a local SQLite database. The dashboard surfaces this as badges on each invoice row and in the detail panel — "last exported Jul 9" — so staff can see at a glance what has already been sent without re-running unnecessarily.

---

## Scope

- Per-invoice `exported_at` and `netsuite_at` timestamps, always updated to most recent event
- Badges visible in the master list and detail panel
- SQLite persistence that survives container restarts
- No full audit log, no per-user attribution, no history view

---

## Storage

**File:** `app/tracking.py`
**Database:** `.tmp/tracking.db` (already volume-mounted in `docker-compose.yml`)

### Schema

```sql
CREATE TABLE IF NOT EXISTS invoice_events (
    transaction_id TEXT NOT NULL,
    event_type     TEXT NOT NULL CHECK(event_type IN ('exported', 'netsuite')),
    occurred_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invoice_events_tx ON invoice_events(transaction_id);
```

One row per event. Multiple export rows per invoice are allowed — queries always fetch the MAX `occurred_at` per `(transaction_id, event_type)`.

### API

```python
def record_events(transaction_ids: list[str], event_type: str) -> None:
    """Insert one row per transaction_id for the given event_type."""

def get_latest_events(transaction_ids: list[str]) -> dict[str, dict]:
    """
    Return {transaction_id: {"exported_at": str|None, "netsuite_at": str|None}}
    for each id in transaction_ids. Missing ids get None values.
    """
```

SQLite opened with `check_same_thread=False` and `PRAGMA journal_mode=WAL` for safe concurrent access from FastAPI's threadpool.

---

## Backend

### `app/tracking.py` (new)

Owns database initialisation, `record_events`, and `get_latest_events`. Called by routes — not by the Crstl client or export module.

### `app/main.py` changes

**`GET /api/invoices`**
After loading invoices from `_cache`, calls `get_latest_events([inv["transaction_id"] for inv in invoices])` and merges the result into each invoice dict:

```python
inv["exported_at"] = events[tx_id]["exported_at"]   # None if never
inv["netsuite_at"] = events[tx_id]["netsuite_at"]   # None if never
```

**`POST /api/export`**
After generating and returning the CSV, calls `record_events(exported_ids, "exported")`. If `body.ids` is None (export all), `exported_ids` is all transaction IDs currently in cache.

**`POST /api/netsuite`** (stub, unchanged for now)
When the real connector is built, it calls `record_events(pushed_ids, "netsuite")` after a successful push.

### `app/export.py` — no changes

Export module stays pure (bytes in, bytes out). Tracking is the route's responsibility.

---

## Frontend

### Master list row

Two small badges appended after the Status badge column, rendered only when the field is set:

| Field | Badge style | Text |
|---|---|---|
| `exported_at` | Gray pill | `↓ Jul 9` |
| `netsuite_at` | Blue pill | `NS Jul 9` (future) |

Dates formatted with the existing `fmtTime()` helper (short month + day only, no year).

### Detail panel

Below Transaction ID, a new "Export history" section:

```
Last exported:   Jul 9, 2:14pm
NetSuite push:   —
```

Shows `—` for fields that are null.

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| `.tmp/` directory missing on startup | `tracking.py` creates it; SQLite creates the file on first write |
| SQLite write fails during export | Exception logged, HTTP response still returned (CSV download proceeds) |
| `get_latest_events` fails | Logged; invoices returned without tracking fields (badges absent, not broken) |
| MOCK_DATA mode | Tracking still works — mock invoices have real `transaction_id` values |

---

## Testing

- `tests/test_tracking.py` — unit tests for `record_events` and `get_latest_events` using a temp file DB
- `tests/test_api.py` — add: after `POST /api/export`, `GET /api/invoices` returns invoices with `exported_at` set

---

## Out of Scope

- Full audit log / history view
- Per-user attribution
- Undo / clear tracking
- Tracking for the manual CSV export in `tools/generate_accounting_csv.py`
