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
