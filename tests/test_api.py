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
def client(monkeypatch, tmp_path):
    # Prevent .env MOCK_DATA=true from bypassing the patched CrstlClient
    monkeypatch.delenv("MOCK_DATA", raising=False)
    # Isolate tracking DB per test — must be set before TestClient starts the lifespan
    monkeypatch.setenv("TRACKING_DB", str(tmp_path / "tracking.db"))
    with patch("app.main.CrstlClient") as MockClient:
        mock_instance = MagicMock()
        mock_instance.fetch_invoices.return_value = MOCK_INVOICES
        MockClient.return_value = mock_instance

        from app.main import app, _cache, _netsuite_state
        with TestClient(app) as c:
            # Reset cache after startup so each test controls its own state
            _cache["invoices"] = []
            _cache["last_synced"] = None
            _cache["status"] = "never"
            _netsuite_state["last_generated"] = None
            _netsuite_state["path"] = None
            _netsuite_state["count"] = 0
            _netsuite_state["skipped"] = 0
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
