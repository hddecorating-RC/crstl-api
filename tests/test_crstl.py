import pytest
from unittest.mock import patch
from app.crstl import CrstlClient


SAMPLE_DETAIL = {
    "metadata": {
        "id": "6a6b84c9f971616e2921bbea",
        "reference_id": "INV40855335",
        "source_document_reference_id": "40855335",
        "trading_partner_name": "Home Depot Canada",
        "created_at": "2026-07-30T17:07:21.959Z",
        "state": {"value": "Accepted"},
        "value": 950.0,
    },
    "file": {
        "generic_json_edi": {
            "heading": {
                "invoice_number": "INV40855335",
                "invoice_date": "2026-07-30",
                "purchase_order_number": "40855335",
            },
            "detail": {
                "baseline_item_data_invoice_loop": [
                    {"baseline_item_data_invoice": {
                        "line_item_number": "10",
                        "quantity_invoiced": 40,
                        "unit_price": 18.75,
                        "quantity_unit_code": "EA",
                        "vendors_part_number": "069556585997",
                    }},
                    {"baseline_item_data_invoice": {
                        "line_item_number": "20",
                        "quantity_invoiced": 2,
                        "unit_price": 100.00,
                        "quantity_unit_code": "EA",
                    }},
                ]
            },
            "summary": {
                "total_monetary_value_summary": {"total_amount": 950.0},
            },
        }
    },
}


def test_requires_api_key():
    with pytest.raises(ValueError):
        CrstlClient(base_url="https://api.crstl.so/v2", api_key="")


def test_sends_api_key_header():
    client = CrstlClient(base_url="https://api.crstl.so/v2", api_key="ct_live_test")
    assert client.session.headers["x-crstl-api-key"] == "ct_live_test"
    assert "Authorization" not in client.session.headers

    from unittest.mock import MagicMock
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {"data": {"transactions": []}}
    with patch.object(client.session, "get", return_value=mock_resp) as mock_get:
        client._fetch_all_transactions()
        mock_get.assert_called_once()


def test_extract_invoice_fields():
    client = CrstlClient(base_url="https://api.crstl.so/v2", api_key="ct_live_test")
    result = client._extract_invoice_fields(SAMPLE_DETAIL)
    assert result["transaction_id"] == "6a6b84c9f971616e2921bbea"
    assert result["invoice_number"] == "INV40855335"
    assert result["po_number"] == "40855335"
    assert result["trading_partner"] == "Home Depot Canada"
    assert result["invoice_date"] == "2026-07-30"
    assert result["status"] == "Accepted"
    assert result["total_amount"] == 950.0
    assert result["subtotal"] == 950.0  # 40*18.75 + 2*100
    assert result["tax_amount"] == 0.0
    assert result["allowance_amount"] == 0.0
    assert result["charge_amount"] == 0.0
    assert result["currency"] == "CAD"
    assert len(result["invoice_lines"]) == 2
    assert result["invoice_lines"][0]["line_amount"] == 750.0
    assert result["invoice_lines"][0]["description"] == "069556585997"


def test_extract_derives_tax_from_total_minus_net():
    """Real HD Canada 810: subtotal $48, allowance $0.60, total $53.56 → tax $6.16"""
    detail = {
        "metadata": {"id": "x", "reference_id": "INV1", "value": 53.56, "state": {"value": "Accepted"}},
        "file": {"generic_json_edi": {
            "heading": {"invoice_date": "2026-07-30"},
            "detail": {"baseline_item_data_invoice_loop": [
                {"baseline_item_data_invoice": {"line_item_number": "10", "quantity_invoiced": "2", "unit_price": "24"}},
            ]},
            "summary": {
                "total_monetary_value_summary": {"total_amount": 53.56},
                "service_promotion_allowance_or_charge_information_loop": [
                    {"service_promotion_allowance_or_charge_information": {
                        "allowance_or_charge_indicator": "A", "amount": 0.60}},
                ],
            },
        }},
    }
    client = CrstlClient(base_url="https://api.crstl.so/v2", api_key="ct_live_test")
    result = client._extract_invoice_fields(detail)
    assert result["subtotal"] == 48.0
    assert result["allowance_amount"] == 0.6
    assert result["tax_amount"] == 6.16
    assert result["total_amount"] == 53.56


def test_extract_falls_back_to_total_when_no_lines():
    detail = {
        "metadata": {"id": "x", "reference_id": "INV1", "value": 500.0, "state": {"value": "Draft"}},
        "file": {"generic_json_edi": {"heading": {}, "detail": {}, "summary": {}}},
    }
    client = CrstlClient(base_url="https://api.crstl.so/v2", api_key="ct_live_test")
    result = client._extract_invoice_fields(detail)
    assert result["subtotal"] == 500.0
    assert result["total_amount"] == 500.0
