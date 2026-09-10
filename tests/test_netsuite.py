import json
import pytest
from unittest.mock import patch

SAMPLE_INVOICE = {
    "transaction_id": "tx-001",
    "source_document_id": "src-001",
    "product": "Drape Panel",
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
    "dropship_blinds": {"QC": "blind-qc", "ON": "blind-on"},
    "item": "Drapery Panels",
    "product_items": {"Blind": "Blinds Item", "Drape Panel": "Drapery Panels"},
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


def test_dropship_blind_routes_to_blinds_customer(mock_config):
    """A dropship BLIND bills to the province's blinds customer; tax still follows
    the province (unchanged)."""
    from app.netsuite import transform_invoice
    inv = {**SAMPLE_INVOICE, "product": "Blind"}
    lines = transform_invoice(inv, province="QC", store=None)
    assert lines is not None
    assert lines[0]["customer_id"] == "blind-qc"     # blinds customer, not cust-qc
    assert lines[0]["tax_code"] == "GST+QST"          # tax unchanged -- by province


def test_dropship_panel_routes_to_panels_customer(mock_config):
    from app.netsuite import transform_invoice
    inv = {**SAMPLE_INVOICE, "product": "Drape Panel"}
    lines = transform_invoice(inv, province="QC", store=None)
    assert lines[0]["customer_id"] == "cust-qc"      # existing (panels) customer


def test_dropship_unknown_product_is_skipped(mock_config):
    """Mixed/Unknown can't be routed to a blinds-vs-panels customer -> skipped."""
    from app.netsuite import transform_invoice
    for prod in ("Unknown", "Mixed", "", None):
        inv = {**SAMPLE_INVOICE, "product": prod}
        assert transform_invoice(inv, province="QC", store=None) is None


def test_dsd_ignores_product_routing(mock_config):
    """DSD is drapery only -- it always uses its store customer regardless of the
    product field."""
    from app.netsuite import transform_invoice
    inv = {**SAMPLE_INVOICE, "product": "Blind"}   # even if mislabeled
    lines = transform_invoice(inv, province="ON", store="VAUGHAN")
    assert lines[0]["customer_id"] == "cust-vaughan"


def test_dropship_blind_without_blinds_customer_is_skipped(mock_config):
    """A blind in a province that has no blinds customer configured is skipped,
    not booked to the panels customer by accident."""
    from app.netsuite import transform_invoice
    inv = {**SAMPLE_INVOICE, "product": "Blind"}
    # SAMPLE_CONFIG has no BC in dropship_blinds; add BC dropship province first
    import app.netsuite as ns
    cfg = dict(SAMPLE_CONFIG)
    cfg["dropship_provinces"] = {**cfg["dropship_provinces"],
                                 "BC": {"customer_id": "cust-bc", "tax_code": "GST", "tax_rate": 0.05}}
    from unittest.mock import patch
    with patch("app.netsuite._load_config", return_value=cfg):
        assert transform_invoice(inv, province="BC", store=None) is None


def test_dropship_blind_uses_blinds_merchandise_item(mock_config):
    """A blind books the blinds merchandise item, not Drapery Panels (so blinds
    revenue lands in the blinds GL account)."""
    from app.netsuite import transform_invoice
    inv = {**SAMPLE_INVOICE, "product": "Blind"}
    lines = transform_invoice(inv, province="QC", store=None)
    assert lines[0]["item"] == "Blinds Item"


def test_dropship_panel_uses_drapery_item(mock_config):
    from app.netsuite import transform_invoice
    inv = {**SAMPLE_INVOICE, "product": "Drape Panel"}
    lines = transform_invoice(inv, province="QC", store=None)
    assert lines[0]["item"] == "Drapery Panels"


def test_dsd_uses_drapery_item(mock_config):
    """DSD is drapery -> Drapery Panels item regardless."""
    from app.netsuite import transform_invoice
    inv = {**SAMPLE_INVOICE, "product": "Drape Panel"}
    lines = transform_invoice(inv, province="ON", store="VAUGHAN")
    assert lines[0]["item"] == "Drapery Panels"


def test_external_id_rejects_unsafe_source_document_id():
    from app.netsuite import external_id_for
    for bad in ["x?replace=none&", "a/b", "has space", "colon:id", "x" * 65]:
        with pytest.raises(ValueError):
            external_id_for({"source_document_id": bad})
    # a clean Mongo-style id is fine
    assert external_id_for({"source_document_id": "6aa1b5e6956e73c8eaa65c52"}) == "CRSTL-6aa1b5e6956e73c8eaa65c52"


def test_amount_flag_soft_per_channel_bounds():
    from app.netsuite import amount_flag
    cfg = {"amount_guards": {"dsd": {"floor": 10, "ceiling": 75000},
                             "dropship": {"floor": 2, "ceiling": 10000}}}
    assert amount_flag(50000, "dsd", cfg) is None            # within DSD range
    assert amount_flag(200000, "dsd", cfg) == "above_ceiling"
    assert amount_flag(5, "dsd", cfg) == "below_floor"
    assert amount_flag(50000, "dropship", cfg) == "above_ceiling"  # huge for dropship
    assert amount_flag(1, "dropship", cfg) == "below_floor"
    assert amount_flag(100, "dropship", cfg) is None
    assert amount_flag(100, "nochannel", cfg) is None        # no bounds -> no flag
    assert amount_flag(None, "dsd", cfg) is None
