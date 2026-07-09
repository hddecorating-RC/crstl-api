# Invoice Dashboard — Design Spec
**Date:** 2026-07-09
**Status:** Approved

## Overview

A lightweight internal web dashboard for HD Decorating that surfaces Crstl EDI 810 invoice data daily. Accounting and operations staff can view invoice status (draft vs sent), search and filter, inspect line-item detail, and manually trigger CSV exports for NetSuite import. A stub button for direct NetSuite push is included for when that connector is built.

---

## Stack

| Layer | Choice | Reason |
|---|---|---|
| Backend | FastAPI (Python) | Fits existing Python scripts; thin layer |
| Frontend | Tailwind CDN + Alpine.js | Polished UI without a build step |
| Scheduler | APScheduler (in-process) | No external cron needed |
| Deploy | Docker on Proxmox CT | Portable; same image migrates to Azure |
| Auth | None (URL access) | Internal only for now |
| Future auth | Azure Easy Auth (SSO) | Zero code change when migrating to Azure |

---

## File Structure

```
crstl-api/
├── app/
│   ├── main.py             # FastAPI app — routes + APScheduler
│   ├── crstl.py            # Crstl API client (auth, fetch, cache)
│   ├── export.py           # CSV generation
│   └── static/
│       └── index.html      # Single-page UI
├── tools/                  # Existing scripts (unchanged, imported by app/)
├── .env
├── docker-compose.yml
└── Dockerfile
```

---

## Backend

### Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/invoices` | Returns cached invoice list |
| `POST` | `/api/sync` | Force re-fetches from Crstl, updates cache |
| `POST` | `/api/export` | Generates CSV, returns as file download |
| `POST` | `/api/netsuite` | Stub — returns HTTP 501 with "coming soon" |

### Caching (`crstl.py`)

Invoices are fetched from Crstl and held in memory. On startup, the cache is populated immediately. APScheduler refreshes it daily at 7:00am. `POST /api/sync` triggers an out-of-cycle refresh. Cache stores the raw invoice list plus a `last_synced` timestamp returned with every `GET /api/invoices` response.

### Scheduler

APScheduler runs inside the FastAPI process (no external cron). Single job: `fetch_and_cache_invoices()` at 07:00 daily. If the container restarts mid-day, the cache repopulates on first `GET /api/invoices` request (blocking, ~5–10s).

### Export (`export.py`)

Reads from the in-memory cache (not a fresh API call). Generates a dated CSV (`invoices_YYYY-MM-DD.csv`) with columns: Invoice Number, PO Number, Trading Partner, Invoice Date, Due Date, Status, Subtotal, Tax Amount, Total Amount, Currency, Transaction ID. Returns as a `Content-Disposition: attachment` response for browser download.

When rows are selected in the UI, `POST /api/export` accepts an optional `ids` list to export a subset.

---

## Frontend (`index.html`)

Single HTML file served by FastAPI. No build step — Tailwind and Alpine.js loaded from CDN.

### Layout

```
┌─────────────────────────────────────────────────┐
│ HD Decorating · Invoice Dashboard   Last sync:… │  ← top bar
├──────┬──────┬──────────────┬────────────────────┤
│Draft │Sent  │ Total Value  │ Last Export        │  ← stat cards
├──────┴──────┴──────────────┴────────────────────┤
│ 🔍 Search…          [All] [Draft] [Sent]        │  ← toolbar
│ (2 selected) [Export Selected] [Push to NS]     │  ← multi-select bar
├────────────────────────┬────────────────────────┤
│ ☐ INV-041  PO-98801… │ INV-2025-041           │
│ ☑ INV-040  PO-98756… │ Status: Draft          │  ← master/detail
│ ☐ INV-039  PO-98734… │ PO: PO-98801           │
│                        │ Due: Aug 7, 2026       │
│                        │ Subtotal: $4,180       │
│                        │ Tax:      $320         │
│                        │ Total:    $4,500       │
│                        │ ─ Line Items ─         │
│                        │ Installation ×10 $3,500│
│                        │ Materials      $680    │
├────────────────────────┴────────────────────────┤
│ [Export All & Download CSV] [Refresh] [NetSuite]│  ← footer actions
└─────────────────────────────────────────────────┘
```

### Components

**Top bar** — app title, last sync timestamp, live/error status dot.

**Stat cards** — Draft count (amber), Sent count (green), Total Value, Last Export time + success/fail. Clicking Draft or Sent card sets that filter.

**Toolbar** — search input filters client-side on invoice #, PO #. Filter pills: All / Draft / Sent. Multi-select action bar appears when ≥1 checkbox is checked; shows count, "Export Selected" button, and disabled "Push to NetSuite" button.

**Master list (left)** — scrollable. Columns: checkbox, Invoice #, PO #, Invoice Date, Total, Status badge. Clicking a row loads its detail in the right panel and highlights the row.

**Detail panel (right)** — always visible. Empty state ("Select an invoice") when nothing selected. Shows: invoice #, status badge, PO #, due date, subtotal, tax, total, trading partner, transaction ID, line items table. No close button — clicking another row replaces it.

**Footer** — "Export All & Download CSV" (triggers `POST /api/export`), "Refresh" (triggers `POST /api/sync` then reloads), disabled "Push to NetSuite" with "coming soon" badge.

### Alpine.js Behaviour

- Invoice data loaded on `x-init` via `fetch('/api/invoices')`
- Auto-refresh every 60 seconds via `setInterval`
- Search filters the list reactively (client-side, no API call)
- Filter pill updates `activeFilter` state; list filters accordingly
- Checkbox selection tracked in `selected` Set; multi-select bar shown/hidden via `selected.size > 0`
- Export button shows spinner during fetch, triggers file download on response
- Toast notification on successful export ("CSV downloaded")
- Sync button shows spinner, reloads invoice data on completion

---

## Deployment

### Dockerfile

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### docker-compose.yml

```yaml
services:
  dashboard:
    build: .
    ports:
      - "8000:8000"
    env_file: .env
    restart: unless-stopped
```

### Proxmox CT Setup

- Ubuntu 22.04 LXC container
- Docker + Docker Compose installed
- Clone repo into `/opt/crstl-api`
- `docker compose up -d`
- Access at `http://<CT-IP>:8000` on internal network

### Azure Migration Path (future)

1. Push image to Azure Container Registry
2. Deploy to Azure App Service (Container)
3. Enable Easy Auth with Azure AD — no code changes
4. Move secrets to Azure App Service environment variables

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| Crstl auth fails on startup | App starts; cache empty; dashboard shows "Sync failed" with retry button |
| Detail fetch fails for one invoice | Logged, skipped; rest of list loads normally |
| Export with empty cache | Returns 503 with message to trigger sync first |
| NetSuite endpoint called | Returns HTTP 501 `{"message": "NetSuite connector not yet configured"}` |
| Container restart | Cache repopulates on first request (~5–10s); spinner shown |

---

## Future: NetSuite Connector

When the NetSuite REST connector is built, `POST /api/netsuite` accepts:
- No body: pushes all invoices
- `{"ids": [...]}`: pushes selected invoices

The "Push to NetSuite" button in the multi-select bar and footer activates automatically once the endpoint returns 200 instead of 501.

---

## Out of Scope

- User accounts or per-user history
- Email delivery of CSVs (accounting downloads manually for now)
- Invoice editing or status changes via the dashboard
- Mobile layout (internal tool, desktop only)
