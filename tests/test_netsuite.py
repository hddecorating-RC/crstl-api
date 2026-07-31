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
    assert header.split(",") == COLUMNS


def test_netsuite_csv_one_row_per_record():
    from app.netsuite_csv import build_netsuite_csv
    csv_bytes = build_netsuite_csv([SAMPLE_RECORD, SAMPLE_RECORD])
    lines = csv_bytes.decode().splitlines()
    assert len(lines) == 3  # header + 2 rows


def test_netsuite_csv_row_values():
    import csv as csv_mod
    import io as io_mod
    from app.netsuite_csv import build_netsuite_csv
    csv_bytes = build_netsuite_csv([SAMPLE_RECORD])
    reader = csv_mod.DictReader(io_mod.StringIO(csv_bytes.decode()))
    row = next(reader)
    assert row["External ID"] == "PO-12345"
    assert row["Customer"] == "cust-vaughan"
    assert row["Rate"] == "18000.0"
    assert row["Tax Code"] == "HST-ON"
    assert row["Currency"] == "CAD"


def test_fetch_po_provinces_extracts_store_and_province():
    from unittest.mock import patch
    from app.crstl import CrstlClient

    client = CrstlClient(
        base_url="https://api.crstl.ai/v2",
        api_key="ct_live_test",
    )

    mock_transactions = [
        {"id": "850-001", "reference_id": "PO-40850625"},
        {"id": "850-002", "reference_id": "PO-537608514"},
    ]
    mock_detail_vaughan = {
        "file": {"generic_json_edi": {"heading": {"ship_to": {
            "state_province": "ON",
            "name": "VAUGHAN STOCK AND FLOW - 7275",
        }}}}
    }
    mock_detail_dropship = {
        "file": {"generic_json_edi": {"heading": {"ship_to": {
            "state_province": "QC",
            "name": "GELINAS ANICK",
        }}}}
    }

    with patch.object(client, "_fetch_all_transactions", return_value=mock_transactions), \
         patch.object(client, "_fetch_transaction_detail", side_effect=[mock_detail_vaughan, mock_detail_dropship]):
        result = client.fetch_po_provinces()

    assert result["PO-40850625"]["province"] == "ON"
    assert result["PO-40850625"]["store"] == "VAUGHAN"
    assert result["PO-537608514"]["province"] == "QC"
    assert result["PO-537608514"]["store"] is None
