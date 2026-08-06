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


def test_extract_categorizes_sac_codes_per_hd_spec():
    """Real HD Canada 810 (Ontario dropship): subtotal $48, discount C300 $0.60,
    HST tax H680 $6.16, total $53.56. Tax comes from the H680 SAC entry
    directly, not derived from total-minus-net."""
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
                        "allowance_or_charge_indicator": "A", "service_promotion_allowance_or_charge_code": "C300", "amount": 0.60}},
                    {"service_promotion_allowance_or_charge_information": {
                        "allowance_or_charge_indicator": "C", "service_promotion_allowance_or_charge_code": "H680", "amount": 6.16}},
                ],
            },
        }},
    }
    client = CrstlClient(base_url="https://api.crstl.so/v2", api_key="ct_live_test")
    result = client._extract_invoice_fields(detail)
    assert result["subtotal"] == 48.0
    assert result["discount_amount"] == 0.60      # C300 classified as discount
    assert result["allowance_amount"] == 0.0
    assert result["tax_amount"] == 6.16           # H680 classified as HST/QST tax
    assert result["tax_breakdown"] == {"HST_QST": 6.16}
    assert result["discrepancy"] == 0.0           # reconciles cleanly
    assert result["freight_amount"] == 0.0
    assert result["fee_amount"] == 0.0
    assert result["total_amount"] == 53.56
    # allowances_charges list is enriched with label + category
    ac = result["allowances_charges"]
    assert len(ac) == 2
    assert ac[0]["code"] == "C300" and ac[0]["category"] == "discount" and ac[0]["label"] == "Discount"
    assert ac[1]["code"] == "H680" and ac[1]["category"] == "tax"      and ac[1]["label"] == "HST/QST Tax"


def test_extract_wholesale_invoice_multiple_categories():
    """Real HD Canada 810 (Calgary wholesale AB): subtotal 10,273.56, allowances
    E210+H090+H000 = 590.73, GST tax D360 = 484.14, total 10,166.97. Verifies
    the classification correctly splits allowances from tax."""
    detail = {
        "metadata": {"id": "x", "reference_id": "INV2", "value": 10166.97, "state": {"value": "Accepted"}},
        "file": {"generic_json_edi": {
            "heading": {"invoice_date": "2026-07-30"},
            "detail": {"baseline_item_data_invoice_loop": [
                {"baseline_item_data_invoice": {"line_item_number": "10", "quantity_invoiced": "1", "unit_price": "10273.56"}},
            ]},
            "summary": {
                "total_monetary_value_summary": {"total_amount": 10166.97},
                "service_promotion_allowance_or_charge_information_loop": [
                    {"service_promotion_allowance_or_charge_information": {
                        "allowance_or_charge_indicator": "A", "service_promotion_allowance_or_charge_code": "E210", "amount": 359.57}},
                    {"service_promotion_allowance_or_charge_information": {
                        "allowance_or_charge_indicator": "A", "service_promotion_allowance_or_charge_code": "H090", "amount": 102.74}},
                    {"service_promotion_allowance_or_charge_information": {
                        "allowance_or_charge_indicator": "A", "service_promotion_allowance_or_charge_code": "H000", "amount": 128.42}},
                    {"service_promotion_allowance_or_charge_information": {
                        "allowance_or_charge_indicator": "C", "service_promotion_allowance_or_charge_code": "D360", "amount": 484.14}},
                ],
            },
        }},
    }
    client = CrstlClient(base_url="https://api.crstl.so/v2", api_key="ct_live_test")
    result = client._extract_invoice_fields(detail)
    assert result["allowance_amount"] == 590.73    # E210 + H090 + H000
    assert result["discount_amount"] == 0.0
    assert result["tax_amount"] == 484.14          # D360 GST alone
    assert result["tax_breakdown"] == {"GST": 484.14}
    # Sanity: subtotal - allowance + tax = total
    assert round(result["subtotal"] - result["allowance_amount"] + result["tax_amount"], 2) == result["total_amount"]


def test_extract_flags_discrepancy_when_sac_incomplete():
    """Dropship invoices commonly omit the tax SAC code — HD bakes tax into
    the total without emitting a corresponding D360/H680. We do NOT derive
    the missing tax (that would hide the gap). Instead we report tax as 0
    per the raw EDI and flag the invoice with a positive discrepancy so
    accounting can spot which invoices need investigation."""
    detail = {
        "metadata": {"id": "x", "reference_id": "INV_DROP", "value": 53.56, "state": {"value": "Draft"}},
        "file": {"generic_json_edi": {
            "heading": {},
            "detail": {"baseline_item_data_invoice_loop": [
                {"baseline_item_data_invoice": {"line_item_number": "10", "quantity_invoiced": "2", "unit_price": "24"}},
            ]},
            "summary": {
                "total_monetary_value_summary": {"total_amount": 53.56},
                # C300 discount only — no tax SAC entry, tax is implicit in total
                "service_promotion_allowance_or_charge_information_loop": [
                    {"service_promotion_allowance_or_charge_information": {
                        "allowance_or_charge_indicator": "A", "service_promotion_allowance_or_charge_code": "C300", "amount": 0.60}},
                ],
            },
        }},
    }
    client = CrstlClient(base_url="https://api.crstl.so/v2", api_key="ct_live_test")
    result = client._extract_invoice_fields(detail)
    assert result["tax_amount"] == 0.0          # nothing in the raw EDI to report
    assert result["tax_breakdown"] == {}
    assert result["discrepancy"] == 6.16        # 53.56 − (48 − 0.60 + 0) — unexplained gap


def test_extract_zero_discrepancy_when_invoice_reconciles_fully():
    """An invoice with no SAC entries and total == subtotal reconciles cleanly."""
    detail = {
        "metadata": {"id": "x", "reference_id": "INV_CLEAN", "value": 100.0, "state": {"value": "Draft"}},
        "file": {"generic_json_edi": {
            "heading": {},
            "detail": {"baseline_item_data_invoice_loop": [
                {"baseline_item_data_invoice": {"line_item_number": "10", "quantity_invoiced": "1", "unit_price": "100"}},
            ]},
            "summary": {"total_monetary_value_summary": {"total_amount": 100.0}},
        }},
    }
    client = CrstlClient(base_url="https://api.crstl.so/v2", api_key="ct_live_test")
    result = client._extract_invoice_fields(detail)
    assert result["discrepancy"] == 0.0


def test_extract_unmapped_code_falls_back_to_indicator():
    """A SAC code not in the HD spec should still show up in the output,
    classified by its indicator (A → allowance, C → fee) so nothing silently
    disappears."""
    detail = {
        "metadata": {"id": "x", "reference_id": "INV3", "value": 100.0, "state": {"value": "Draft"}},
        "file": {"generic_json_edi": {
            "heading": {},
            "detail": {"baseline_item_data_invoice_loop": [
                {"baseline_item_data_invoice": {"line_item_number": "10", "quantity_invoiced": "1", "unit_price": "100"}},
            ]},
            "summary": {
                "total_monetary_value_summary": {"total_amount": 100.0},
                "service_promotion_allowance_or_charge_information_loop": [
                    {"service_promotion_allowance_or_charge_information": {
                        "allowance_or_charge_indicator": "C", "service_promotion_allowance_or_charge_code": "Z999", "amount": 5.0}},
                ],
            },
        }},
    }
    client = CrstlClient(base_url="https://api.crstl.so/v2", api_key="ct_live_test")
    result = client._extract_invoice_fields(detail)
    assert result["fee_amount"] == 5.0             # unmapped C-indicator → fee
    assert result["tax_amount"] == 0.0             # not tax
    assert result["allowances_charges"][0]["category"] == "fee"


def test_extract_falls_back_to_total_when_no_lines():
    detail = {
        "metadata": {"id": "x", "reference_id": "INV1", "value": 500.0, "state": {"value": "Draft"}},
        "file": {"generic_json_edi": {"heading": {}, "detail": {}, "summary": {}}},
    }
    client = CrstlClient(base_url="https://api.crstl.so/v2", api_key="ct_live_test")
    result = client._extract_invoice_fields(detail)
    assert result["subtotal"] == 500.0
    assert result["total_amount"] == 500.0


def test_extract_preserves_metadata_fields_when_sac_loop_present():
    """Regression guard: a loop-local variable named `meta` (or any other name
    matching an outer binding — `heading`, `summary`, `detail_body`) inside the
    SAC classification loop would silently shadow the metadata dict and cause
    every metadata-sourced field on the return dict to fall to empty. That
    was the actual production bug that left 69/105 invoices with empty
    transaction_id and collapsed the frontend row list.

    Any invoice with at least one SAC entry must still round-trip its
    metadata fields intact."""
    detail = {
        "metadata": {
            "id": "tx-abc-123",
            "reference_id": "INV-9999",
            "source_document_id": "src-777",
            "source_document_type": "850",
            "source_document_reference_id": "PO-9999",
            "trading_partner_name": "Home Depot Canada",
            "trading_partner_flavor": "Dropship",
            "value": 100.0,
            "state": {"value": "Accepted"},
            "created_at": "2026-08-06T12:00:00Z",
        },
        "file": {"generic_json_edi": {
            "heading": {"invoice_date": "2026-08-06"},
            "detail": {"baseline_item_data_invoice_loop": [
                {"baseline_item_data_invoice": {"line_item_number": "10", "quantity_invoiced": "2", "unit_price": "50"}},
            ]},
            "summary": {
                "total_monetary_value_summary": {"total_amount": 100.0},
                # At least one SAC entry — this is what triggers the loop that
                # previously shadowed `meta`.
                "service_promotion_allowance_or_charge_information_loop": [
                    {"service_promotion_allowance_or_charge_information": {
                        "allowance_or_charge_indicator": "A",
                        "service_promotion_allowance_or_charge_code": "C300",
                        "amount": 1.25}},
                ],
            },
        }},
    }
    client = CrstlClient(base_url="https://api.crstl.so/v2", api_key="ct_live_test")
    result = client._extract_invoice_fields(detail)
    assert result["transaction_id"] == "tx-abc-123"
    assert result["invoice_number"] == "INV-9999"
    assert result["po_number"] == "PO-9999"
    assert result["source_document_id"] == "src-777"
    assert result["source_document_type"] == "850"
    assert result["trading_partner"] == "Home Depot Canada"
    assert result["trading_partner_flavor"] == "Dropship"
    assert result["status"] == "Accepted"
    assert result["created_at"] == "2026-08-06T12:00:00Z"
