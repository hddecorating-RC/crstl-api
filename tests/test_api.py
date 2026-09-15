import io
from openpyxl.utils import get_column_letter

import pytest
from openpyxl.utils import get_column_letter
from openpyxl import load_workbook
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.report import XLSX_MEDIA_TYPE

MOCK_INVOICES = [
    {
        "transaction_id": "tx-001",
        "source_document_id": "src-tx-001",
        "invoice_number": "INV-001",
        "po_number": "PO-123",
        "trading_partner": "Home Depot",
        "invoice_date": "2026-07-01",
        "due_date": "2026-08-01",
        # Crstl returns "Accepted" or "Draft"; "Open" was invented here and
        # exists in no real payload. Reporting filters on Accepted, so a
        # fictional status made this fixture silently unreportable.
        "status": "Accepted",
        "subtotal": 4180.0,
        "tax_amount": 320.0,
        "total_amount": 4500.0,
        "currency": "USD",
        "created_at": "2026-07-01T00:00:00Z",
        "invoice_lines": [],
    }
]


def _raw_810(tx_id, subtotal, tax, *, invoice_date="2026-07-01",
             flavor="HD Canada Dropship", province="ON", po="PO-123"):
    """A minimal 810 detail payload in the shape app/report.py extracts from.

    The export re-reads each invoice from Crstl rather than trusting the cached
    figures, so a test that exercises /api/export has to stand up the raw
    payload too — a MagicMock detail would extract into a workbook of
    MagicMocks and assert nothing real.
    """
    return {
        "metadata": {
            "id": tx_id,
            "reference_id": f"INV-{tx_id}",
            "source_document_reference_id": po,
            "trading_partner_flavor": flavor,
            "state": {"value": "Accepted"},
            "value": round(subtotal + tax, 2),
            "created_at": f"{invoice_date}T00:00:00Z",
        },
        "file": {"generic_json_edi": {
            "heading": {
                "invoice_number": f"INV-{tx_id}",
                "invoice_date": invoice_date,
                "ship_to": {"state_province": province},
            },
            "detail": {"baseline_item_data_invoice_loop": [
                {"baseline_item_data_invoice": {
                    "quantity_invoiced": "1", "unit_price": f"{subtotal}"}}
            ]},
            # Dropship carries tax in TXI, not SAC. VA is HD's code for HST.
            "summary": {"tax_information": (
                [{"tax_type_code": "VA", "monetary_amount": f"{tax}"}] if tax else []
            )},
        }},
    }


# Amounts per transaction id, mirroring the cached fixtures below.
_RAW_AMOUNTS = {"tx-001": (4180.0, 320.0)}


def _detail_side_effect(tx_id):
    subtotal, tax = _RAW_AMOUNTS.get(tx_id, (100.0, 0.0))
    return _raw_810(tx_id, subtotal, tax)


def _invoice_rows(content):
    """Data rows of the workbook's Invoices sheet — header and Total excluded."""
    ws = load_workbook(io.BytesIO(content))["Invoices"]
    return [r for r in ws.iter_rows(min_row=2, values_only=True)
            if r and r[0] and r[0] != "Total"]


def _invoice_numbers(content):
    return [r[0] for r in _invoice_rows(content)]


def _invoice_header(content):
    """Header labels of the Invoices sheet.

    Assertions index by label rather than by position: the sheet has gained
    columns twice (Product, then Ship Date), and each time every positional
    assertion downstream of the insert broke without the figures themselves
    being wrong."""
    ws = load_workbook(io.BytesIO(content))["Invoices"]
    return [c.value for c in ws[1]]


@pytest.fixture
def client(monkeypatch, tmp_path):
    # Prevent .env MOCK_DATA=true from bypassing the patched CrstlClient
    monkeypatch.delenv("MOCK_DATA", raising=False)
    # Isolate tracking DB per test — must be set before TestClient starts the lifespan
    monkeypatch.setenv("TRACKING_DB", str(tmp_path / "tracking.db"))
    # Finale is a real third-party API and the sync now calls it. Stubbed here
    # so the suite neither reaches the network nor depends on whoever runs it
    # having Finale credentials.
    monkeypatch.setattr("app.main.FinaleClient.configured", staticmethod(lambda: False))
    with patch("app.main.CrstlClient") as MockClient:
        mock_instance = MagicMock()
        mock_instance.fetch_invoices.return_value = MOCK_INVOICES
        # A real dict, not a MagicMock: _refresh_cache now keeps this map so the
        # export can resolve a Dropship province without re-crawling every 850.
        mock_instance.fetch_po_provinces.return_value = {}
        mock_instance._fetch_transaction_detail.side_effect = _detail_side_effect
        MockClient.return_value = mock_instance

        from app.main import app, _cache, _netsuite_state
        with TestClient(app) as c:
            # Reset cache after startup so each test controls its own state
            _cache["invoices"] = []
            _cache["po_provinces"] = {}
            _cache["last_synced"] = None
            _cache["status"] = "never"
            _netsuite_state["last_generated"] = None
            _netsuite_state["path"] = None
            _netsuite_state["count"] = 0
            _netsuite_state["skipped"] = 0
            _netsuite_state["error"] = None
            _netsuite_state["generating"] = False
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
    data = resp.json()
    assert data["ok"] is True
    assert data["last_synced"] is not None
    assert data["status"] == "ok"


def test_export_all_returns_xlsx(client):
    # Pre-populate cache via sync
    client.post("/api/sync")
    resp = client.post("/api/export", json={})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == XLSX_MEDIA_TYPE
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].endswith('.xlsx"')
    assert _invoice_numbers(resp.content) == ["INV-tx-001"]


def test_export_subset_by_ids(client):
    client.post("/api/sync")
    resp = client.post("/api/export", json={"ids": ["tx-001"]})
    assert resp.status_code == 200
    assert _invoice_numbers(resp.content) == ["INV-tx-001"]


def test_export_reports_the_810_figures_not_the_cached_ones(client):
    """The cache infers Dropship tax from a province rate table; the workbook
    reads the TXI segment HD actually sent. This asserts the workbook is built
    from the payload, so the two can never silently diverge behind one
    filename."""
    client.post("/api/sync")
    resp = client.post("/api/export", json={"ids": ["tx-001"]})
    row = _invoice_rows(resp.content)[0]
    at = _invoice_header(resp.content).index
    assert row[at("Type")] == "Dropship"
    # fetch_po_provinces is stubbed empty here, so there is no 850 to read the
    # ordered items from. A Dropship invoice with no PO on file reports
    # Unknown rather than defaulting to the product line we sell more of.
    assert row[at("Product")] == "Unknown"
    assert row[at("Province")] == "ON"
    assert row[at("Subtotal")] == 4180.0    # subtotal from the line loop
    assert row[at("Tax")] == 320.0          # tax from TXI, not from a rate table
    assert row[at("Total")] == 4500.0       # metadata.value, HD's stated total


def test_export_502s_when_crstl_returns_nothing(client):
    """An empty workbook reads as "a quiet day" to whoever opens it. If every
    detail fetch failed, that must surface as an error instead."""
    from app.main import _cache
    client.post("/api/sync")
    with patch("app.main.rows_for_transactions", return_value=[]):
        resp = client.post("/api/export", json={})
    assert resp.status_code == 502
    assert "no detail" in resp.json()["message"]


def test_export_empty_cache_returns_503(client):
    # Don't sync — cache is empty
    resp = client.post("/api/export", json={})
    assert resp.status_code == 503


def test_netsuite_push_dry_run(client):
    client.post("/api/sync")
    resp = client.post("/api/netsuite")   # dry_run defaults True -> writes nothing
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "dry"
    assert "summary" in body and "results" in body
    # a dry run must not set any per-invoice netsuite_at
    invoices = client.get("/api/invoices").json()["invoices"]
    assert all(inv["netsuite_at"] is None for inv in invoices)
    # ...and it is logged for the dashboard
    latest = client.get("/api/netsuite-push/latest").json()
    assert latest["mode"] == "dry" and latest["running"] is False


def test_netsuite_push_scoped_by_ids(client):
    """Single (flyout) and bulk (selected rows) push the same endpoint with ids."""
    client.post("/api/sync")
    invoices = client.get("/api/invoices").json()["invoices"]
    assert invoices
    tid = invoices[0]["transaction_id"]
    resp = client.post("/api/netsuite", json={"dry_run": True, "ids": [tid]})
    assert resp.status_code == 200
    assert [r["transaction_id"] for r in resp.json()["results"]] == [tid]


def test_netsuite_push_live_requires_ids(client):
    """An unauthenticated live push MUST name invoices; an unscoped live POST is
    refused (can't book the whole batch from one call)."""
    client.post("/api/sync")
    resp = client.post("/api/netsuite", json={"dry_run": False})   # no ids
    assert resp.status_code == 400
    assert "name the invoices" in resp.json()["message"]


def test_netsuite_push_rejects_nonpositive_limit(client):
    resp = client.post("/api/netsuite", json={"dry_run": True, "limit": 0})
    assert resp.status_code == 422   # Pydantic ge=1 — no more "0 means no cap"


def test_netsuite_push_error_detail_is_sanitized(client, monkeypatch):
    """Raw NetSuite/exception detail must not reach the unauthenticated caller."""
    def fake_push(invoices, **kw):
        return {"mode": "live", "unresolved": [],
                "summary": {"built": 1, "sent": 0, "failed": 1, "skipped_no_map": 0},
                "results": [{"transaction_id": "X", "channel": "dsd", "where": "VAUGHAN",
                             "status": "failed", "error": "NetSuite 400: {internal field detail}"}]}
    monkeypatch.setattr("app.main.push_invoices", fake_push)
    resp = client.post("/api/netsuite", json={"dry_run": False, "ids": ["X"]})
    err = resp.json()["results"][0]["error"]
    assert "internal field detail" not in err
    assert err == "upsert failed — see server logs"


def test_export_sets_exported_at(client):
    client.post("/api/sync")
    client.post("/api/export", json={})

    resp = client.get("/api/invoices")
    invoices = resp.json()["invoices"]
    assert len(invoices) > 0
    assert invoices[0]["exported_at"] is not None
    assert invoices[0]["netsuite_at"] is None


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


def _scheduled_jobs(monkeypatch, tmp_path):
    """Start the app lifespan with a stubbed scheduler and return the cron
    kwargs each job registered with, keyed by job id."""
    monkeypatch.delenv("MOCK_DATA", raising=False)
    monkeypatch.setenv("TRACKING_DB", str(tmp_path / "tracking.db"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    jobs: dict[str, dict] = {}

    with patch("app.main.CrstlClient") as MockClient, \
         patch("app.main.AsyncIOScheduler") as MockScheduler:
        mock_instance = MagicMock()
        mock_instance.fetch_invoices.return_value = MOCK_INVOICES
        MockClient.return_value = mock_instance

        def add_job(func, trigger, **kwargs):
            jobs[kwargs["id"]] = {"trigger": trigger, **kwargs}

        MockScheduler.return_value.add_job.side_effect = add_job

        from app.main import app
        with TestClient(app):
            pass

    return jobs


def test_digest_scheduled_weekdays_only(monkeypatch, tmp_path):
    """The digest must not fire Sat/Sun — Friday's late invoices ride along
    with Monday's send because tracking.db still lists them as unemailed."""
    jobs = _scheduled_jobs(monkeypatch, tmp_path)
    digest = jobs["daily_digest"]
    assert digest["day_of_week"] == "mon-fri"
    assert (digest["hour"], digest["minute"]) == (7, 15)
    assert digest["timezone"] == "America/Toronto"


def test_refresh_runs_every_day(monkeypatch, tmp_path):
    """The cache refresh runs on weekends too so Monday's digest has current data
    (only the email and the NetSuite push are weekday-gated)."""
    jobs = _scheduled_jobs(monkeypatch, tmp_path)
    assert "day_of_week" not in jobs["daily_refresh"]


# ── Accepted-only reporting ────────────────────────────────────────────────
# The warehouse resubmits invoices when it catches a mistake, and each attempt
# lands as its own Crstl record. Only the acknowledged one may reach accounting.

def _mixed_status_invoices():
    def inv(tx, num, status, total):
        return {
            "transaction_id": tx, "invoice_number": num, "po_number": "PO-1",
            "trading_partner": "Home Depot Canada", "province": "ON",
            "invoice_date": "2026-08-24", "due_date": "2026-09-23",
            "status": status, "subtotal": total, "tax_amount": 0.0,
            "total_amount": total, "currency": "CAD",
            "created_at": "2026-08-24T00:00:00Z", "invoice_lines": [],
            "allowances_charges": [],
        }
    # one accepted invoice plus two superseded drafts of the same PO
    return [inv("tx-acc", "INV-1", "Accepted", 100.0),
            inv("tx-dr1", "INV-1", "Draft", 100.0),
            inv("tx-dr2", "INV-1", "Draft", 100.0)]


def test_reportable_keeps_accepted_and_drops_drafts():
    from app.main import _reportable
    kept = _reportable(_mixed_status_invoices())
    assert [i["transaction_id"] for i in kept] == ["tx-acc"]


def test_reportable_withholds_unknown_status_and_warns(capsys):
    """An unrecognised state must not reach accounting silently. If Crstl ever
    adds a status that means "good", this warning is how we find out rather
    than invoices quietly vanishing from the digest."""
    from app.main import _reportable
    rows = _mixed_status_invoices() + [{"transaction_id": "tx-new", "status": "Submitted"}]
    kept = _reportable(rows)
    assert [i["transaction_id"] for i in kept] == ["tx-acc"]
    out = capsys.readouterr().out
    assert "Submitted" in out and "REPORTABLE_STATUSES" in out
    # a plain Draft is expected and must not generate noise
    assert "Draft" not in out


def test_bulk_export_excludes_drafts(client):
    """A bulk export is a report, so it carries Accepted only."""
    from app.main import _cache
    client.post("/api/sync")
    with patch.dict(_cache, {"invoices": _mixed_status_invoices()}):
        content = client.post("/api/export", json={}).content
    numbers = _invoice_numbers(content)
    assert numbers == ["INV-tx-acc"], f"expected only the Accepted invoice, got {numbers}"


def test_explicit_id_selection_is_honoured_even_for_a_draft(client):
    """Picking specific invoices on the dashboard is a deliberate act, so it is
    not second-guessed — only the automatic bulk report filters."""
    from app.main import _cache
    client.post("/api/sync")
    with patch.dict(_cache, {"invoices": _mixed_status_invoices()}):
        content = client.post("/api/export", json={"ids": ["tx-dr1"]}).content
    assert _invoice_numbers(content) == ["INV-tx-dr1"]


def _so_ready_invoices():
    """Two Accepted, on/after-cutoff, mappable dropship invoices for the SO digest."""
    base = {"status": "Accepted", "due_date": "", "store": None, "province": "ON",
            "product": "Drape Panel", "invoice_date": "2026-09-12"}
    return [
        {**base, "transaction_id": "so-1", "source_document_id": "sd1", "invoice_number": "INV-SO-1",
         "po_number": "PO1", "subtotal": 100.0, "allowance_amount": 5.19, "discount_amount": 0.0,
         "total_amount": 107.14},
        {**base, "transaction_id": "so-2", "source_document_id": "sd2", "invoice_number": "INV-SO-2",
         "po_number": "PO2", "subtotal": 200.0, "allowance_amount": 10.38, "discount_amount": 0.0,
         "total_amount": 214.27},
    ]


def test_so_digest_lists_pushed_sos_gaps_and_marks_reported(client, monkeypatch):
    """The daily digest lists SOs actually pushed to NetSuite (with a direct link),
    flags invoiced-but-no-SO as an issue, and marks the reported ones so they don't
    repeat tomorrow -- sourced from durable receipts, not in-memory state."""
    from app.main import _cache, _send_daily_digest
    from app import tracking
    tracking.init_db()
    monkeypatch.setenv("MAIL_RECIPIENTS", "accounting@example.com")
    monkeypatch.setenv("NETSUITE_ACCOUNT_ID", "734463")
    # so-1 was pushed (receipt + stored SO internal id); so-2 was not (a gap).
    tracking.record_events(["so-1"], "netsuite")
    tracking.record_netsuite_push("CRSTL-sd1", "22699999", "T1")
    with patch.dict(_cache, {"invoices": _so_ready_invoices()}), \
         patch("app.main.send_mail") as mail:
        result = _send_daily_digest()
    assert result["count"] == 1 and result["gaps"] == 1
    body = mail.call_args.kwargs["body_html"]
    assert "Product" in body and "Drape Panel" in body        # blinds-vs-drapes summary table
    assert "Province" not in body                             # province table removed per feedback
    assert "INV-SO-2" in body                                  # the gap called out in issues
    assert "INV-SO-1" not in body                              # created-SO detail lives in the Excel, not the email
    # reported SO is marked so it won't repeat in the next digest
    assert tracking.get_latest_events(["so-1"])["so-1"]["so_digest_at"] is not None
    # the gap is NOT marked (still needs an SO)
    assert tracking.get_latest_events(["so-2"])["so-2"]["so_digest_at"] is None


def test_so_digest_workbook_adds_linked_so_column(monkeypatch):
    """The digest Excel is the export workbook (Invoices sheet) plus a
    'Netsuite SO created' column whose cell links straight to the SO."""
    from app.main import _so_digest_workbook
    from openpyxl import Workbook
    # stand-in export workbook: one invoice row + a Total row, matching the real shape
    wb = Workbook(); ws = wb.active; ws.title = "Invoices"
    ws.append(["Invoice", "Type", "Total"])
    ws.append(["INV-SO-1", "Dropship", 100.0])
    ws.append(["Total", "", 100.0])
    ws.auto_filter.ref = "A1:C2"
    buf = io.BytesIO(); wb.save(buf); wb_bytes = buf.getvalue()
    monkeypatch.setattr("app.main._workbook_for", lambda invs: wb_bytes)

    url = "https://734463.app.netsuite.com/app/accounting/transactions/salesord.nl?id=999"
    out = _so_digest_workbook([{"invoice_number": "INV-SO-1"}],
                              {"INV-SO-1": ("2026-09-14", url)})
    ws2 = load_workbook(io.BytesIO(out))["Invoices"]
    headers = [c.value for c in ws2[1]]
    assert "Netsuite SO created" in headers                     # new column added
    col = headers.index("Netsuite SO created") + 1
    cell = ws2.cell(row=2, column=col)
    assert cell.value == "2026-09-14"
    assert cell.hyperlink and "id=999" in cell.hyperlink.target  # links straight to the SO


def test_sync_survives_finale_being_down(monkeypatch, client):
    """Finale is a second external service on the sync path. If it fails the
    Ship Date column goes blank; it must not take the dashboard with it."""
    down = MagicMock()
    down.configured.return_value = True
    down.side_effect = RuntimeError("finale is down")
    monkeypatch.setattr("app.main.FinaleClient", down)
    resp = client.post("/api/sync")
    assert resp.status_code == 200
    assert client.get("/api/invoices").json()["status"] == "ok"


def test_automation_status_and_toggle(client):
    resp = client.get("/api/automation")
    assert resp.status_code == 200
    jobs = {j["id"]: j for j in resp.json()["jobs"]}
    assert set(jobs) == {"daily_refresh", "netsuite_push", "daily_digest", "finale_push"}
    assert jobs["netsuite_push"]["enabled"] is False   # off by default
    assert jobs["daily_digest"]["enabled"] is True
    # toggle push on, verify persisted
    assert client.post("/api/automation", json={"job": "netsuite_push", "enabled": True}).status_code == 200
    jobs2 = {j["id"]: j for j in client.get("/api/automation").json()["jobs"]}
    assert jobs2["netsuite_push"]["enabled"] is True
    client.post("/api/automation", json={"job": "netsuite_push", "enabled": False})
    # unknown job -> 404
    assert client.post("/api/automation", json={"job": "nope", "enabled": True}).status_code == 404


def test_automation_logs(client):
    resp = client.get("/api/automation/logs")
    assert resp.status_code == 200
    assert "runs" in resp.json()


def test_netsuite_push_runs_before_business_hours(monkeypatch, tmp_path):
    """Auto-push fires 5:00 AM ET (before accounting works), AFTER the 4:45 sync
    (fresh data) and before the 7:15 digest (which reports it), so a scheduled
    push never collides with a manual NetSuite entry."""
    jobs = _scheduled_jobs(monkeypatch, tmp_path)
    push, refresh = jobs["netsuite_push"], jobs["daily_refresh"]
    assert push["day_of_week"] == "mon-fri"
    assert (push["hour"], push["minute"]) == (5, 0)
    assert (refresh["hour"], refresh["minute"]) == (4, 45)
    assert refresh["hour"] < push["hour"]        # sync precedes the push


def test_reportable_defers_send_success_without_warning(capsys):
    """Send_Success is a transient CRSTL state (810 transmitted to HD, not yet
    acknowledged) -- deferred like a Draft, and NOT warned about. We only book
    invoices HD has Accepted."""
    from app.main import _reportable
    rows = [{"status": "Send_Success", "transaction_id": "t1"},
            {"status": "Accepted", "transaction_id": "t2"}]
    kept = _reportable(rows)
    assert [r["transaction_id"] for r in kept] == ["t2"]      # Send_Success deferred
    assert "Send_Success" not in capsys.readouterr().out      # known state -> no warning


def test_live_push_fires_digest_dry_run_does_not(client, monkeypatch):
    """The digest fires the moment a live push lands SOs (no waiting for 7:15); a
    dry run never sends. Guarded by the auto-digest toggle."""
    from app.main import _run_netsuite_push, _cache
    monkeypatch.setattr("app.main._auto_digest_enabled", lambda: True)
    fake = {"mode": "live", "summary": {"sent": 2, "failed": 0}, "unresolved": [],
            "results": [], "blocked": None}
    with patch.dict(_cache, {"invoices": []}), \
         patch("app.main.push_invoices", return_value=fake), \
         patch("app.main._send_digest_safe") as digest:
        _run_netsuite_push(live=True, ids=["x"], limit=None)
    digest.assert_called_once()

    with patch.dict(_cache, {"invoices": []}), \
         patch("app.main.push_invoices", return_value={**fake, "mode": "dry", "summary": {"sent": 0, "failed": 0}}), \
         patch("app.main._send_digest_safe") as digest2:
        _run_netsuite_push(live=False, ids=None, limit=None)
    digest2.assert_not_called()


def test_live_push_invoices_in_finale_only_when_enabled(monkeypatch):
    """Finale invoicing rides the NetSuite push: the ids that actually SENT get a
    Finale invoice in the same run -- only when the toggle is on, never on a dry run."""
    from app.main import _run_netsuite_push, _cache
    monkeypatch.setattr("app.main._auto_digest_enabled", lambda: False)
    fake = {"mode": "live", "summary": {"sent": 2, "failed": 0}, "unresolved": [], "blocked": None,
            "results": [{"transaction_id": "x", "status": "sent"}, {"transaction_id": "y", "status": "sent"},
                        {"transaction_id": "z", "status": "skipped_exists"}]}
    with patch.dict(_cache, {"invoices": []}), patch("app.main.push_invoices", return_value=fake), \
         patch("app.main._finale_enabled", return_value=True), \
         patch("app.main._run_finale_push_safe") as fin:
        _run_netsuite_push(live=True, ids=["x", "y", "z"], limit=None)
    fin.assert_called_once_with(["x", "y"])                 # only what SENT, not the skipped one
    with patch.dict(_cache, {"invoices": []}), patch("app.main.push_invoices", return_value=fake), \
         patch("app.main._finale_enabled", return_value=False), \
         patch("app.main._run_finale_push_safe") as fin2:
        _run_netsuite_push(live=True, ids=["x"], limit=None)
    fin2.assert_not_called()
    with patch.dict(_cache, {"invoices": []}), \
         patch("app.main.push_invoices", return_value={**fake, "mode": "dry", "summary": {"sent": 0, "failed": 0}}), \
         patch("app.main._finale_enabled", return_value=True), \
         patch("app.main._run_finale_push_safe") as fin3:
        _run_netsuite_push(live=False, ids=None, limit=None)
    fin3.assert_not_called()


def test_finale_push_safe_honors_cap_and_never_raises(monkeypatch):
    from app.main import _run_finale_push_safe, _cache
    from app import tracking
    tracking.init_db()
    monkeypatch.setattr("app.main._finale_config", lambda: {"enabled": True, "max_per_run": 1, "go_live_after": "2026-09-15"})
    monkeypatch.setattr("app.main.load_refs", lambda: {"automation": {"created_within_days": 365}})
    inv = [{"transaction_id": "a", "created_at": "2026-09-16T01:00:00Z"}, {"transaction_id": "b", "created_at": "2026-09-16T01:00:00Z"}]
    with patch.dict(_cache, {"invoices": inv}), patch("app.main._run_finale_push") as run:
        _run_finale_push_safe(["a", "b"])
    run.assert_called_once_with(True, ["a", "b"], None, max_per_run=1)  # cap handed to the engine (counts writes)
    with patch.dict(_cache, {"invoices": inv}), patch("app.main._run_finale_push", side_effect=RuntimeError("boom")):
        _run_finale_push_safe(["a"])                        # failure is swallowed, never fails the push


def test_finale_push_safe_applies_the_finale_floor_and_window(monkeypatch):
    """A manual NetSuite push is unlimited by design; the Finale ride-along is not:
    only ids inside Finale's own floor + rolling window are invoiced, the rest are
    left to the warehouse. Unknown ids (not in the cache) are out of scope."""
    from app.main import _run_finale_push_safe, _cache
    from app import tracking
    tracking.init_db()
    monkeypatch.setattr("app.main._finale_config", lambda: {"enabled": True, "max_per_run": 75, "go_live_after": "2026-09-15"})
    monkeypatch.setattr("app.main.load_refs", lambda: {"automation": {"go_live_after": "2026-09-11", "created_within_days": 365}})
    inv = [{"transaction_id": "old", "created_at": "2026-09-12T01:00:00Z"},     # NetSuite floor ok, Finale floor not
           {"transaction_id": "new", "created_at": "2026-09-16T01:00:00Z"}]
    with patch.dict(_cache, {"invoices": inv}), patch("app.main._run_finale_push") as run, \
         patch("app.tracking.record_job_run") as job:
        _run_finale_push_safe(["old", "new", "ghost"])
    run.assert_called_once_with(True, ["new"], None, max_per_run=75)
    assert any("2 sent to NetSuite left alone" in str(c.args) for c in job.call_args_list)
    with patch.dict(_cache, {"invoices": inv}), patch("app.main._run_finale_push") as run2:
        _run_finale_push_safe(["old"])
    run2.assert_not_called()


def test_one_finale_run_at_a_time_across_every_entry_point(monkeypatch, client):
    """The poll, the NetSuite ride-along and the manual endpoints share one lock: a
    second run is refused (409 / skipped), never interleaved; the poll's own passes
    re-enter the lock on the same thread."""
    import threading
    from app import main as m
    from app import tracking
    tracking.init_db()
    fake = {"mode": "dry", "unresolved": [], "results": [], "summary": {"built": 0, "posted": 0, "draft": 0, "failed": 0}}
    # hold the lock from another thread, as a running poll would
    held, release = threading.Event(), threading.Event()
    def hold():
        with m._finale_run("poll"):
            held.set(); release.wait(5)
    th = threading.Thread(target=hold); th.start(); held.wait(5)
    try:
        with patch.dict(m._cache, {"invoices": [], "po_provinces": {}}), patch("app.main.push_finale_invoices", return_value=fake):
            assert client.post("/api/finale", json={"dry_run": True}).status_code == 409
            assert client.get("/api/finale-push/latest").json()["running"] is True
        with patch("app.main.push_nonedi_invoices", return_value=fake), patch("app.finale.FinaleClient.configured", return_value=True), \
             patch("app.finale.FinaleClient"):
            assert client.post("/api/finale/nonedi", json={"dry_run": True}).status_code == 409
        with patch.dict(m._cache, {"invoices": [{"transaction_id": "a", "created_at": "2026-09-16T01:00:00Z"}]}), \
             patch("app.main._finale_config", return_value={"enabled": True, "go_live_after": "2026-09-15", "max_per_run": 75}), \
             patch("app.main.load_refs", return_value={"automation": {"created_within_days": 365}}), \
             patch("app.main.push_finale_invoices", return_value=fake) as push, patch("app.tracking.record_job_run") as job:
            m._run_finale_push_safe(["a"])                       # ride-along: skipped, not queued, not raised
        push.assert_not_called()
        assert any("in progress" in str(c.args) for c in job.call_args_list)
        with patch("app.main._finale_enabled", return_value=True), patch("app.main._finale_poll_passes") as passes, \
             patch("app.tracking.record_job_run") as job2:
            m._run_finale_push_job()                             # a second poll: skipped
        passes.assert_not_called()
        assert any(c.args[1] == "skipped" and "in progress" in c.args[2] for c in job2.call_args_list)
    finally:
        release.set(); th.join(5)
    assert client.get("/api/finale-push/latest").json()["running"] is False
    # released: the poll's passes run nested inside its own hold (re-entrant), and the endpoint works again
    with patch("app.main._finale_enabled", return_value=True), patch("app.main._finale_edi_pass") as edi, \
         patch("app.main._run_nonedi_push") as ne, patch("app.main._finale_config", return_value={"enabled": True}):
        m._run_finale_push_job()
    edi.assert_called_once(); ne.assert_called_once()
    with patch.dict(m._cache, {"invoices": [], "po_provinces": {}}), patch("app.main.push_finale_invoices", return_value=fake), \
         patch("app.tracking.record_job_run"):
        assert client.post("/api/finale", json={"dry_run": True}).status_code == 200


def test_finale_endpoint_live_requires_ids_and_dry_run_previews(client):
    r = client.post("/api/finale", json={"dry_run": False})
    assert r.status_code == 400 and "ids" in r.json()["message"]
    fake = {"mode": "dry", "unresolved": [], "results": [{"transaction_id": "t", "status": "built"}],
            "summary": {"built": 1, "posted": 0, "draft": 0, "failed": 0}}
    with patch("app.main.push_finale_invoices", return_value=fake), \
         patch("app.tracking.record_job_run"):
        r2 = client.post("/api/finale", json={"dry_run": True})
    assert r2.status_code == 200 and r2.json()["summary"]["built"] == 1
    assert client.get("/api/finale-push/latest").json()["mode"] == "dry"


def test_so_digest_reports_finale_line_and_holds(client, monkeypatch):
    """One headline line for Finale, and drafts called out under Issues; nothing
    Finale-related when the feature is off and nothing was invoiced."""
    from app.main import _cache, _send_daily_digest
    from app import tracking
    tracking.init_db()
    monkeypatch.setenv("MAIL_RECIPIENTS", "accounting@example.com")
    monkeypatch.setenv("NETSUITE_ACCOUNT_ID", "734463")
    tracking.record_events(["so-1"], "netsuite")
    tracking.record_netsuite_push("CRSTL-sd1", "22699999", "T1")
    tracking.record_finale_invoice("so-1", "PO-1", "100407", "/i/100407", "PO-1-1", "draft")
    with patch.dict(_cache, {"invoices": _so_ready_invoices()}), patch("app.main.send_mail") as mail, \
         patch("app.main._finale_enabled", return_value=True):
        _send_daily_digest()
    body = mail.call_args.kwargs["body_html"]
    assert "Finale:" in body and "0 invoice(s) posted" in body and "1 held as draft" in body
    assert "PO-1-1" in body                                   # the draft is named under Issues


def test_so_digest_workbook_adds_finale_column(monkeypatch):
    from app.main import _so_digest_workbook
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active; ws.title = "Invoices"
    ws.append(["Invoice", "Type", "Total"]); ws.append(["INV-SO-1", "Dropship", 100.0]); ws.append(["Total", "", 100.0])
    ws.auto_filter.ref = "A1:C2"
    buf = io.BytesIO(); wb.save(buf)
    monkeypatch.setattr("app.main._workbook_for", lambda invs: buf.getvalue())
    out = _so_digest_workbook([{"invoice_number": "INV-SO-1"}], {"INV-SO-1": ("2026-09-15", "")},
                              {"INV-SO-1": ("PO-1-1", "posted")})
    ws2 = load_workbook(io.BytesIO(out))["Invoices"]
    headers = [c.value for c in ws2[1]]
    assert headers[-2:] == ["Netsuite SO created", "Finale invoice"]
    assert ws2.cell(row=2, column=len(headers)).value == "PO-1-1 (posted)"
    assert ws2.auto_filter.ref.endswith(f"{get_column_letter(len(headers))}2")



def test_finale_poll_job_gates_refreshes_and_invoices_only_unfinaled(monkeypatch):
    """The 15-min poll: no-op when off; when on it refreshes incrementally, then invoices
    the Accepted-not-yet-invoiced set under the automation guards + the finale cap."""
    from app.main import _run_finale_push_job, _cache
    from app import tracking
    tracking.init_db()
    invs = [{"transaction_id": "a", "source_document_id": "sa", "status": "Accepted", "subtotal": 10,
             "created_at": "2026-09-15T10:00:00Z", "invoice_date": "2026-09-15"},
            {"transaction_id": "b", "source_document_id": "sb", "status": "Accepted", "subtotal": 10,
             "created_at": "2026-09-15T10:00:00Z", "invoice_date": "2026-09-15"}]
    with patch("app.main._finale_enabled", return_value=False), patch("app.main._run_finale_push") as run:
        _run_finale_push_job()
    run.assert_not_called()
    with patch.dict(_cache, {"invoices": invs}), patch("app.main._finale_enabled", return_value=True), \
         patch("app.main._refresh_new_accepted", return_value=0) as refresh, \
         patch("app.main._finale_config", return_value={"enabled": True, "max_per_run": 75}), \
         patch("app.tracking.get_unfinaled_ids", return_value=["b"]), \
         patch("app.main._run_finale_push") as run2:
        _run_finale_push_job()
    refresh.assert_called_once()
    run2.assert_called_once_with(True, ["b"], None, max_per_run=75)    # only the un-invoiced one; cap to the engine
    with patch.dict(_cache, {"invoices": invs}), patch("app.main._finale_enabled", return_value=True), \
         patch("app.main._refresh_new_accepted", return_value=0), \
         patch("app.main._finale_config", return_value={"enabled": True, "max_per_run": 1}), \
         patch("app.tracking.get_unfinaled_ids", return_value=["a", "b"]), \
         patch("app.main._run_finale_push") as run3:
        _run_finale_push_job()
    run3.assert_called_once_with(True, ["a", "b"], None, max_per_run=1)  # the ENGINE caps, on invoices it would create


def test_refresh_new_accepted_fetches_only_changed_and_merges(monkeypatch):
    """One list call; details only for new/changed transactions; missing 850s fetched
    for just those POs; cache entries replaced in place, new ones appended."""
    from app.main import _refresh_new_accepted, _cache
    class FakeCrstl:
        def __init__(self): self.calls = []
        def list_transaction_states(self):
            return {"a": {"state": "Accepted", "updated_at": "", "po_number": "PO-A"},   # unchanged
                    "b": {"state": "Accepted", "updated_at": "", "po_number": "PO-B"},   # Draft -> Accepted
                    "c": {"state": "Draft",    "updated_at": "", "po_number": "PO-C"}}   # new
        def fetch_invoices(self, only_ids=None):
            self.calls.append(("fetch_invoices", sorted(only_ids)))
            return [{"transaction_id": t, "po_number": f"PO-{t.upper()}", "status": "Accepted" if t == "b" else "Draft",
                     "subtotal": 1, "discrepancy": 0} for t in only_ids]
        def fetch_po_provinces(self, only_pos=None):
            self.calls.append(("fetch_po_provinces", sorted(only_pos)))
            return {p: {"province": "ON", "store": None, "vendor_items": [], "lines": []} for p in only_pos}
    fake = FakeCrstl()
    monkeypatch.setattr("app.main._mock_mode", lambda: False)
    monkeypatch.setattr("app.main._get_client", lambda: fake)
    start = [{"transaction_id": "a", "po_number": "PO-A", "status": "Accepted"},
             {"transaction_id": "b", "po_number": "PO-B", "status": "Draft"}]
    with patch.dict(_cache, {"invoices": start, "po_provinces": {"PO-A": {"province": "ON", "store": None, "vendor_items": []}}}):
        n = _refresh_new_accepted()
        by = {i["transaction_id"]: i for i in _cache["invoices"]}
        pos = set(_cache["po_provinces"])
    assert n == 2
    assert fake.calls == [("fetch_invoices", ["b", "c"]), ("fetch_po_provinces", ["PO-B", "PO-C"])]
    assert by["b"]["status"] == "Accepted" and "c" in by and by["a"]["status"] == "Accepted"
    assert pos == {"PO-A", "PO-B", "PO-C"} and by["b"]["province"] == "ON"
    # a PO the 4:45 full refresh added WHILE this poll was fetching survives the merge
    class Racing(FakeCrstl):
        def fetch_invoices(self, only_ids=None):
            _cache["po_provinces"]["PO-NEW"] = {"province": "QC"}
            return super().fetch_invoices(only_ids)
    monkeypatch.setattr("app.main._get_client", lambda: Racing())
    with patch.dict(_cache, {"invoices": list(start), "po_provinces": {"PO-A": {"province": "ON"}}}):
        _refresh_new_accepted()
        assert "PO-NEW" in _cache["po_provinces"] and "PO-B" in _cache["po_provinces"]



def test_finale_poll_job_also_runs_the_nonedi_pass(monkeypatch):
    from app.main import _run_finale_push_job, _cache
    from app import tracking
    tracking.init_db()
    with patch.dict(_cache, {"invoices": []}), patch("app.main._finale_enabled", return_value=True), \
         patch("app.main._refresh_new_accepted", return_value=0), \
         patch("app.main._finale_config", return_value={"enabled": True, "max_per_run": 75}), \
         patch("app.tracking.get_unfinaled_ids", return_value=[]), \
         patch("app.main._run_nonedi_push") as nonedi:
        _run_finale_push_job()
    nonedi.assert_called_once_with(True, None, None)
    with patch("app.main._finale_enabled", return_value=False), patch("app.main._run_nonedi_push") as nonedi2:
        _run_finale_push_job()
    nonedi2.assert_not_called()


def test_nonedi_endpoint_and_runner_classify_by_exclusion(client, monkeypatch):
    """The runner feeds the engine Finale's sale-order list and the Crstl PO set; the
    endpoint previews by default and refuses an unscoped live run."""
    from app.main import _cache, _run_nonedi_push
    from app import tracking
    tracking.init_db()
    r = client.post("/api/finale/nonedi", json={"dry_run": False})
    assert r.status_code == 400
    orders = [{"orderId": "538831979", "orderTypeId": "SALES_ORDER", "statusId": "ORDER_LOCKED", "orderDate": "2026-09-16"},   # EDI PO
              {"orderId": "507872-00", "orderTypeId": "SALES_ORDER", "statusId": "ORDER_LOCKED", "orderDate": "2026-09-16",
               "orderUrl": "/hddecorating/api/order/507872-00", "orderRoleList": [{"roleTypeId": "CUSTOMER", "partyId": "100022"}],
               "orderItemList": [{"productUrl": "/p/a", "unitPrice": 10.0, "quantity": 1}]}]
    class FakeFinale:
        account_id = "hddecorating"
        def list_sale_orders(self): return orders
        def party_province_index(self): return {"100022": "ON"}
        def get_order(self, oid): return orders[1]
        def order_invoices(self, o): return []
        def shipment_qty_for_order(self, o): return {"/p/a": 1.0}
    with patch("app.main.FinaleClient", create=True), patch("app.finale.FinaleClient") as FC, \
         patch.dict(_cache, {"invoices": [{"po_number": "538831979"}], "po_provinces": {}}), \
         patch("app.main._finale_config", return_value={"enabled": True, "nonedi_go_live_after": "2026-09-15", "max_per_run": 75}), \
         patch("app.tracking.get_finale_invoices", return_value={}), patch("app.tracking.record_job_run"):
        FC.configured.return_value = True; FC.return_value = FakeFinale()
        out = _run_nonedi_push(False, None, None)
    ids = [x["order_id"] for x in out["results"]]
    assert ids == ["507872-00"]                                       # the EDI PO was excluded
    assert out["results"][0]["status"] == "built" and out["results"][0]["would"] == "posted"
    assert out["summary"]["candidates"] == 1


def test_digest_lists_stale_pickups_and_nonedi_line(client, monkeypatch):
    """An Accepted 810 older than stale_pickup_days with no Finale invoice and no
    shipment in Finale is called out under Issues; recent non-EDI receipts get a line."""
    from app.main import _cache, _send_daily_digest
    from app import tracking
    from datetime import datetime, timezone, timedelta
    tracking.init_db()
    monkeypatch.setenv("MAIL_RECIPIENTS", "accounting@example.com")
    monkeypatch.setenv("NETSUITE_ACCOUNT_ID", "734463")
    invs = _so_ready_invoices()
    old = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in invs: i["created_at"] = old
    tracking.record_events([i["transaction_id"] for i in invs], "netsuite")      # SOs exist, no Finale receipts
    tracking.record_finale_invoice("order:507872-00", "507872-00", "100420", "/i/100420", "507872-00-1", "posted")
    class FakeFinale:
        def get_order(self, po): return {"orderId": po, "shipmentUrlList": []}
        def shipment_qty_for_order(self, o): return None                          # nothing shipped
    with patch.dict(_cache, {"invoices": invs}), patch("app.main.send_mail") as mail, \
         patch("app.main._finale_enabled", return_value=True), \
         patch("app.main._finale_config", return_value={"enabled": True, "stale_pickup_days": 4, "max_per_run": 75}), \
         patch("app.finale.FinaleClient") as FC:
        FC.configured.return_value = True; FC.return_value = FakeFinale()
        _send_daily_digest()
    body = mail.call_args.kwargs["body_html"]
    assert "not shipped in Finale" in body and "6 days" in body and "over 4 days ago" in body
    assert "Finale (non-EDI orders, since" in body and "1 invoice(s) posted" in body and "507872-00" in body
    # the configured threshold is what the text says, and a Finale RECEIPT counts as invoiced
    # even when the batch event write was lost
    tracking.record_finale_invoice(invs[0]["transaction_id"], invs[0]["po_number"], "1", "/i/1", "x-1", "posted")
    with patch.dict(_cache, {"invoices": invs}), patch("app.main.send_mail") as mail2, \
         patch("app.main._finale_enabled", return_value=True), \
         patch("app.main._finale_config", return_value={"enabled": True, "stale_pickup_days": 6, "max_per_run": 75}), \
         patch("app.finale.FinaleClient") as FC:
        FC.configured.return_value = True; FC.return_value = FakeFinale()
        _send_daily_digest()
    body2 = mail2.call_args.kwargs["body_html"]
    assert "over 6 days ago" in body2 and "over 4 days ago" not in body2
    assert str(invs[0]["invoice_number"]) not in body2.split("not shipped in Finale")[-1]   # receipted: not stale
    assert str(invs[1]["invoice_number"]) in body2.split("not shipped in Finale")[-1]       # un-receipted: still stale


def test_digest_nonedi_window_starts_at_the_last_sent_digest(client):
    from app.main import _last_digest_sent_at
    from app import tracking
    tracking.init_db()
    assert _last_digest_sent_at() is None
    tracking.record_job_run("daily_digest", "ok", "nothing to report")
    assert _last_digest_sent_at() is None                       # nothing went out: window does not move
    tracking.record_job_run("daily_digest", "ok", "sent 3 SO(s) [post-push]")
    tracking.record_job_run("daily_digest", "error", "smtp down")
    assert _last_digest_sent_at()                               # the last actual send, errors ignored


def test_dsd_prefill_runs_in_the_finale_job_only_when_configured(monkeypatch):
    from app.main import _run_finale_push_job
    monkeypatch.setattr("app.main._finale_enabled", lambda: True)
    with patch("app.main._finale_edi_pass"), patch("app.main._run_nonedi_push"), \
         patch("app.main._finale_config", return_value={"enabled": True, "dsd_prefill": True}), \
         patch("app.main._run_dsd_prefill") as dsd:
        _run_finale_push_job()
    dsd.assert_called_once_with(True, None, None)
    with patch("app.main._finale_edi_pass"), patch("app.main._run_nonedi_push"), \
         patch("app.main._finale_config", return_value={"enabled": True}), \
         patch("app.main._run_dsd_prefill") as dsd2:
        _run_finale_push_job()
    dsd2.assert_not_called()
    # a DSD failure is isolated, like the other passes
    with patch("app.main._finale_edi_pass"), patch("app.main._run_nonedi_push"), \
         patch("app.main._finale_config", return_value={"enabled": True, "dsd_prefill": True}), \
         patch("app.main._run_dsd_prefill", side_effect=RuntimeError("boom")), \
         patch("app.tracking.record_job_run") as job:
        _run_finale_push_job()
    assert ("finale_dsd", "error") in {(c.args[0], c.args[1]) for c in job.call_args_list}


def test_dsd_endpoint_live_requires_ids_and_dry_run_previews(client):
    r = client.post("/api/finale/dsd", json={"dry_run": False})
    assert r.status_code == 400 and "ASN ids" in r.json()["message"]
    fake = {"mode": "dry", "results": [{"asn_id": "a1", "status": "would_prefill"}],
            "summary": {"candidates": 1, "would_prefill": 1}}
    with patch("app.main._run_dsd_prefill", return_value=fake) as run:
        r2 = client.post("/api/finale/dsd", json={"dry_run": True, "ids": ["a1"]})
    assert r2.status_code == 200 and r2.json()["summary"]["would_prefill"] == 1
    run.assert_called_once_with(False, ["a1"], None)


def test_run_dsd_prefill_applies_the_shared_guards_to_the_automated_pass(monkeypatch):
    """ids=None (the 15-min pass): Accepted 856s after the floor, no receipt, under the
    cap -- and only those get a detail fetch. A named run fetches exactly the ids given."""
    from app.main import _run_dsd_prefill
    from app import tracking
    tracking.init_db()
    states = {"new": {"state": "Accepted", "created_at": "2026-09-16T01:00:00Z", "po_number": "1"},
              "old": {"state": "Accepted", "created_at": "2026-09-01T01:00:00Z", "po_number": "2"},
              "draft": {"state": "Draft", "created_at": "2026-09-16T01:00:00Z", "po_number": "3"}}
    crstl = type("C", (), {"list_transaction_states": lambda self, transaction_type="810": states,
                           "fetch_asn_refs": lambda self, ids: [{"asn_id": i, "po_number": "1", "state": "Accepted",
                                                                 "pro": "3200", "rts": "6100", "pickup_date": ""} for i in ids]})()
    monkeypatch.setattr("app.main._get_client", lambda: crstl)
    monkeypatch.setattr("app.main._finale_config", lambda: {"enabled": True, "dsd_prefill": True, "go_live_after": "2026-09-15", "max_per_run": 5})
    monkeypatch.setattr("app.main.load_refs", lambda: {"automation": {"created_within_days": 365}})
    with patch("app.finale.FinaleClient.configured", return_value=True), patch("app.finale.FinaleClient") as fc, \
         patch("app.main.push_dsd_prefill", return_value={"mode": "dry", "results": [], "summary": {"candidates": 1}}) as push:
        fc.configured.return_value = True
        _run_dsd_prefill(False, None, None)
        assert [a["asn_id"] for a in push.call_args[0][0]] == ["new"]
        _run_dsd_prefill(False, ["old"], None)
        assert [a["asn_id"] for a in push.call_args[0][0]] == ["old"]
