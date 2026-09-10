import json
import pytest
from unittest.mock import patch

SAMPLE_INVOICE = {
    "transaction_id": "tx-001",
    "source_document_id": "src-001",
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
    "dsd_stores": {
        "VAUGHAN": {"customer_id": "cust-vaughan", "province": "ON", "tax_code": "HST-ON", "tax_rate": 0.13},
        "CALGARY": {"customer_id": "cust-calgary", "province": "AB", "tax_code": "GST", "tax_rate": 0.05},
    },
    "dropship_provinces": {
        "QC": {"customer_id": "cust-qc", "tax_code": "GST+QST", "tax_rate": 0.14975},
        "ON": {"customer_id": "cust-on-ds", "tax_code": "HST-ON", "tax_rate": 0.13},
    },
    "item": "Drapery Panels",
    "currency": "CAD",
    "channel_discounts": {
        "dsd": {"item": "-6.19% vendor discounts", "rate": 0.0619, "note": "DSD note"},
        "dropship": {"item": "-5.19% vendor discounts", "rate": 0.0519, "note": "Dropship note"},
    },
}


@pytest.fixture
def mock_config():
    with patch("app.netsuite._load_config", return_value=SAMPLE_CONFIG):
        yield


def test_transform_dsd_vaughan(mock_config):
    """DSD invoice => 2 lines: gross merchandise + one 6.19% discount line, both
    sharing external_id/customer/tax_code; tax computed on the NET."""
    from app.netsuite import transform_invoice
    lines = transform_invoice(SAMPLE_INVOICE, province="ON", store="VAUGHAN")
    assert lines is not None and len(lines) == 2
    merch, disc = lines

    assert merch["external_id"] == "CRSTL-src-001"  # namespaced source_document_id
    assert merch["customer_id"] == "cust-vaughan"
    assert merch["tax_code"] == "HST-ON"
    assert merch["tran_date"] == "2026-07-07"
    assert merch["due_date"] == "2026-08-06"
    assert merch["memo"] == "INV-001 / PO PO-12345"
    assert merch["other_ref_num"] == "PO-12345"
    assert merch["currency"] == "CAD"
    assert merch["item"] == "Drapery Panels"
    assert merch["rate"] == 18000.00 and merch["amount"] == 18000.00
    assert merch["quantity"] == 1
    # gross 18000 * 6.19% = 1114.20; net 16885.80 * 13% = 2195.15 on the merch line
    assert merch["tax_amount"] == 2195.15

    assert disc["item"] == "-6.19% vendor discounts"
    assert disc["rate"] == -1114.20 and disc["amount"] == -1114.20
    assert disc["tax_amount"] == 0
    assert disc["tax_code"] == "HST-ON"          # same code => reduces the taxable base
    assert disc["description"] == "DSD note"
    assert disc["external_id"] == "CRSTL-src-001" # shares the invoice


def test_transform_dropship_quebec(mock_config):
    """Dropship => the 5.19% channel discount (drops freight + RDC), QST tax."""
    from app.netsuite import transform_invoice
    lines = transform_invoice(SAMPLE_INVOICE, province="QC", store=None)
    assert lines is not None and len(lines) == 2
    merch, disc = lines
    assert merch["customer_id"] == "cust-qc"
    assert merch["tax_code"] == "GST+QST"
    assert disc["item"] == "-5.19% vendor discounts"
    # 18000 * 5.19% = 934.20; net 17065.80 * 14.975% = 2555.60
    assert disc["rate"] == -934.20
    assert merch["tax_amount"] == 2555.60


def test_transform_total_reconciles_to_net_plus_tax(mock_config):
    """The invoice total NetSuite will book = gross + discount + tax; the per-line
    fields must sum to it exactly (deterministic, no rounding drift)."""
    from app.netsuite import transform_invoice
    lines = transform_invoice(SAMPLE_INVOICE, province="ON", store="VAUGHAN")
    total = sum(l["amount"] for l in lines) + sum(l["tax_amount"] for l in lines)
    # gross 18000 - discount 1114.20 + tax 2195.15
    assert round(total, 2) == 19080.95


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
    "item":          "Drapery Panels",
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
        "file": {"generic_json_edi": {
            "heading": {"ship_to": {"state_province": "QC", "name": "GELINAS ANICK"}},
            "detail": {"baseline_item_data_loop": [
                {"baseline_item_data": {"vendors_item_number": "138VB48D36WHTC"}}]},
        }}
    }

    with patch.object(client, "_fetch_all_transactions", return_value=mock_transactions), \
         patch.object(client, "_fetch_transaction_detail", side_effect=[mock_detail_vaughan, mock_detail_dropship]):
        result = client.fetch_po_provinces()

    assert result["PO-40850625"]["province"] == "ON"
    assert result["PO-40850625"]["store"] == "VAUGHAN"
    assert result["PO-537608514"]["province"] == "QC"
    assert result["PO-537608514"]["store"] is None
    # The map feeds the workbook's Product column as well as its Province one:
    # an 810 carries no vendor item number, so this is the only place to read it.
    assert result["PO-537608514"]["vendor_items"] == ["138VB48D36WHTC"]
    assert result["PO-40850625"]["vendor_items"] == []


def test_fetch_po_provinces_keeps_a_po_that_has_items_but_no_province():
    """A PO with no ship-to province used to be dropped. Dropping it now would
    blank the Product column too, so one missing field would cost two."""
    from unittest.mock import patch
    from app.crstl import CrstlClient

    client = CrstlClient(base_url="https://api.crstl.ai/v2", api_key="ct_live_test")
    detail = {"file": {"generic_json_edi": {
        "heading": {"ship_to": {"name": "NO PROVINCE ON FILE"}},
        "detail": {"baseline_item_data_loop": [
            {"baseline_item_data": {"vendors_item_number": "72318-109-52-84-404"}}]},
    }}}

    with patch.object(client, "_fetch_all_transactions",
                      return_value=[{"id": "850-003", "reference_id": "PO-1"}]), \
         patch.object(client, "_fetch_transaction_detail", return_value=detail):
        result = client.fetch_po_provinces()

    assert result["PO-1"]["province"] is None
    assert result["PO-1"]["vendor_items"] == ["72318-109-52-84-404"]


def test_fetch_po_provinces_still_drops_a_po_with_neither():
    from unittest.mock import patch
    from app.crstl import CrstlClient

    client = CrstlClient(base_url="https://api.crstl.ai/v2", api_key="ct_live_test")
    with patch.object(client, "_fetch_all_transactions",
                      return_value=[{"id": "850-004", "reference_id": "PO-2"}]), \
         patch.object(client, "_fetch_transaction_detail", return_value={}):
        result = client.fetch_po_provinces()

    assert result == {}


def test_external_id_is_namespaced_source_document_id():
    from app.netsuite import external_id_for
    assert external_id_for({"source_document_id": "abc"}) == "CRSTL-abc"


def test_external_id_requires_source_document_id():
    from app.netsuite import external_id_for
    with pytest.raises(ValueError):
        external_id_for({"source_document_id": ""})


def test_resubmission_shares_external_id(mock_config):
    """Two CRSTL versions of one invoice (different transaction_id, same
    source_document_id) map to the SAME NetSuite externalId, so a re-push updates
    the existing record instead of creating a duplicate."""
    from app.netsuite import transform_invoice
    v1 = {**SAMPLE_INVOICE, "transaction_id": "tx-A", "source_document_id": "SD"}
    v2 = {**SAMPLE_INVOICE, "transaction_id": "tx-B", "source_document_id": "SD"}
    l1 = transform_invoice(v1, province="ON", store="VAUGHAN")
    l2 = transform_invoice(v2, province="ON", store="VAUGHAN")
    assert l1[0]["external_id"] == l2[0]["external_id"] == "CRSTL-SD"
