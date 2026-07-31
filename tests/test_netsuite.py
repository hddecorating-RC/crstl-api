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
    "allowance_amount": 0.0,
    "charge_amount": 0.0,
    "allowances_charges": [],
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
    "allowance_item": "Allowance",
    "charge_item": "Charge",
    "currency": "CAD",
}


@pytest.fixture
def mock_config():
    with patch("app.netsuite._load_config", return_value=SAMPLE_CONFIG):
        yield


def test_transform_wholesale_vaughan(mock_config):
    from app.netsuite import transform_invoice
    lines = transform_invoice(SAMPLE_INVOICE, province="ON", store="VAUGHAN")
    assert lines is not None and len(lines) == 1  # only merchandise line — no allowances/charges
    row = lines[0]
    assert row["external_id"] == "tx-001"  # transaction_id — the only field guaranteed unique
    assert row["customer_id"] == "cust-vaughan"
    assert row["tax_code"] == "HST-ON"
    assert row["rate"] == 18000.00
    assert row["tax_amount"] == 2340.00
    assert row["tran_date"] == "2026-07-07"
    assert row["due_date"] == "2026-08-06"
    assert row["memo"] == "INV-001 / PO PO-12345"
    assert row["other_ref_num"] == "PO-12345"
    assert row["currency"] == "CAD"
    assert row["item"] == "Merchandise Sales"
    assert row["description"] == ""
    assert row["quantity"] == 1


def test_transform_dropship_quebec(mock_config):
    from app.netsuite import transform_invoice
    lines = transform_invoice(SAMPLE_INVOICE, province="QC", store=None)
    assert lines is not None and len(lines) == 1
    assert lines[0]["customer_id"] == "cust-qc"
    assert lines[0]["tax_code"] == "GST+QST"


def test_transform_emits_allowance_and_charge_lines(mock_config):
    """Real HD invoice: subtotal + 3 allowances + 1 charge = 5 lines, all shared external_id."""
    from app.netsuite import transform_invoice
    inv = {
        **SAMPLE_INVOICE,
        "transaction_id": "6a6b84c9f971616e2921bbea",
        "invoice_number": "INV40855335",
        "po_number": "40855335",
        "subtotal": 10273.56,
        "tax_amount": 0.0,
        "total_amount": 10166.97,
        "allowances_charges": [
            {"type": "Allowance", "code": "E210", "amount": 359.57},
            {"type": "Allowance", "code": "H090", "amount": 102.74},
            {"type": "Allowance", "code": "H000", "amount": 128.42},
            {"type": "Charge",    "code": "D360", "amount": 484.14},
        ],
    }
    lines = transform_invoice(inv, province="AB", store="CALGARY")
    assert len(lines) == 5
    # All lines share external_id, customer, tax_code
    assert {l["external_id"] for l in lines} == {"6a6b84c9f971616e2921bbea"}
    assert {l["customer_id"] for l in lines} == {"cust-calgary"}
    assert {l["tax_code"] for l in lines} == {"GST"}
    # Line 0 is merchandise
    assert lines[0]["item"] == "Merchandise Sales"
    assert lines[0]["rate"] == 10273.56
    assert lines[0]["tax_amount"] == 0.0
    # Allowance lines: negative rate, "Allowance" item, code as description, 0 tax
    assert lines[1] == {**lines[1], "item": "Allowance", "description": "E210", "rate": -359.57, "amount": -359.57, "tax_amount": 0}
    assert lines[2]["description"] == "H090" and lines[2]["rate"] == -102.74
    assert lines[3]["description"] == "H000" and lines[3]["rate"] == -128.42
    # Charge line: positive rate, "Charge" item
    assert lines[4]["item"] == "Charge"
    assert lines[4]["description"] == "D360"
    assert lines[4]["rate"] == 484.14
    # Row-sum sanity: sum(rate) + tax matches actual total ($10,166.97)
    computed_total = sum(l["rate"] for l in lines) + lines[0]["tax_amount"]
    assert round(computed_total, 2) == inv["total_amount"]


def test_transform_unknown_province_returns_none(mock_config):
    from app.netsuite import transform_invoice
    assert transform_invoice(SAMPLE_INVOICE, province="XX", store=None) is None


def test_transform_none_province_no_store_returns_none(mock_config):
    from app.netsuite import transform_invoice
    assert transform_invoice(SAMPLE_INVOICE, province=None, store=None) is None


def test_transform_unknown_store_returns_none(mock_config):
    from app.netsuite import transform_invoice
    assert transform_invoice(SAMPLE_INVOICE, province="ON", store="TORONTO") is None


SAMPLE_RECORD = {
    "external_id":   "tx-001",
    "customer_id":   "cust-vaughan",
    "tran_date":     "2026-07-07",
    "due_date":      "2026-08-06",
    "memo":          "PO-12345",
    "other_ref_num": "PO-12345",
    "item":          "Merchandise Sales",
    "description":   "",
    "quantity":      1,
    "rate":          18000.00,
    "amount":        18000.00,
    "tax_code":      "HST-ON",
    "tax_amount":    2340.00,
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
    assert row["External ID"] == "tx-001"
    assert row["Customer"] == "cust-vaughan"
    assert row["Rate"] == "18000.0"
    assert row["Tax Code"] == "HST-ON"
    assert row["Currency"] == "CAD"
    assert row["Description"] == ""


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
