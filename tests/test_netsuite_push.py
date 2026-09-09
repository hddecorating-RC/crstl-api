"""
Tests for the shared push engine (app.netsuite_push). No network or credentials:
transform/config are patched and NetSuite is a fake client. Locks down dry-run
building, the live upsert loop, the unresolved-id block, tracking, and filtering.
"""
import pytest
from unittest.mock import patch

from app.netsuite_push import push_invoices

CONFIG = {
    "dsd_stores": {"VAUGHAN": {"customer_id": "4115", "province": "ON",
                               "tax_code": "CA-HST ONT", "tax_rate": 0.13}},
    "dropship_provinces": {"ON": {"customer_id": "4147", "tax_code": "CA-HST ONT", "tax_rate": 0.13}},
    "item": "Drapery Panels",
    "currency": "CAD",
    "channel_discounts": {
        "dsd": {"item": "-6.19% vendor discounts", "rate": 0.0619, "note": "n"},
        "dropship": {"item": "-5.19% vendor discounts", "rate": 0.0519, "note": "n"},
    },
}

# refs with the dropship discount item still UNRESOLVED (blank id)
REFS_PARTIAL = {
    "item_ids": {"Drapery Panels": "194642", "-6.19% vendor discounts": "194744",
                 "-5.19% vendor discounts": ""},
    "tax_code_ids": {"CA-HST ONT": "468"},
    "class_id": "", "subsidiary_id": "1", "custom_form_id": "101",
}
REFS_FULL = {**REFS_PARTIAL,
             "item_ids": {**REFS_PARTIAL["item_ids"], "-5.19% vendor discounts": "999999"}}

INVOICES = [
    {"transaction_id": "T-DSD", "invoice_number": "INV1", "po_number": "PO1",
     "invoice_date": "2026-09-01", "due_date": "2026-10-01", "subtotal": 100.00,
     "total_amount": 106.00, "store": "VAUGHAN", "province": "ON"},
    {"transaction_id": "T-DROP", "invoice_number": "INV2", "po_number": "PO2",
     "invoice_date": "2026-09-01", "due_date": "2026-10-01", "subtotal": 200.00,
     "total_amount": 210.00, "store": None, "province": "ON"},
    {"transaction_id": "T-NOMAP", "invoice_number": "INV3", "po_number": "PO3",
     "invoice_date": "2026-09-01", "due_date": "", "subtotal": 50.00,
     "total_amount": 50.00, "store": None, "province": "XX"},
]


class FakeClient:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on or set()
    def upsert_invoice(self, payload):
        self.calls.append(payload)
        if payload["externalId"] in self.fail_on:
            raise RuntimeError("boom")
        return {"location": f"/record/v1/invoice/{payload['externalId']}"}


@pytest.fixture(autouse=True)
def _cfg():
    with patch("app.netsuite._load_config", return_value=CONFIG):
        yield


def test_dry_run_builds_and_reconciles_without_sending():
    with patch("app.tracking.record_events") as rec:
        out = push_invoices(INVOICES, live=False, refs=REFS_PARTIAL)
    assert out["mode"] == "dry"
    assert out["summary"] == {"built": 2, "sent": 0, "failed": 0, "skipped_no_map": 1}
    dsd = next(r for r in out["results"] if r["transaction_id"] == "T-DSD")
    assert dsd["channel"] == "dsd" and dsd["status"] == "built"
    assert dsd["gross"] == 100.0 and dsd["discount"] == -6.19 and dsd["net"] == 93.81
    assert dsd["tax"] == 12.20 and dsd["total"] == 106.01
    assert dsd["hd_total"] == 106.00 and dsd["delta"] == 0.01
    nomap = next(r for r in out["results"] if r["transaction_id"] == "T-NOMAP")
    assert nomap["status"] == "skipped_no_map"
    # dropship discount item is unresolved
    assert "item:-5.19% vendor discounts" in out["unresolved"]
    rec.assert_not_called()


def test_live_is_refused_while_ids_unresolved():
    client = FakeClient()
    with patch("app.tracking.record_events") as rec:
        out = push_invoices(INVOICES, live=True, refs=REFS_PARTIAL, client=client)
    assert out.get("blocked") == "unresolved ids"
    assert out["summary"]["sent"] == 0
    assert client.calls == []          # nothing written
    rec.assert_not_called()


def test_live_sends_and_records_tracking():
    client = FakeClient()
    with patch("app.tracking.record_events") as rec:
        out = push_invoices(INVOICES, live=True, refs=REFS_FULL, client=client)
    assert out["mode"] == "live"
    assert out["summary"] == {"built": 2, "sent": 2, "failed": 0, "skipped_no_map": 1}
    assert len(client.calls) == 2
    sent = [r for r in out["results"] if r["status"] == "sent"]
    assert {r["transaction_id"] for r in sent} == {"T-DSD", "T-DROP"}
    assert sent[0]["location"].startswith("/record/v1/invoice/")
    rec.assert_called_once_with(["T-DSD", "T-DROP"], "netsuite")


def test_live_one_failure_does_not_stop_the_batch():
    client = FakeClient(fail_on={"T-DROP"})
    with patch("app.tracking.record_events") as rec:
        out = push_invoices(INVOICES, live=True, refs=REFS_FULL, client=client)
    assert out["summary"] == {"built": 2, "sent": 1, "failed": 1, "skipped_no_map": 1}
    failed = next(r for r in out["results"] if r["status"] == "failed")
    assert failed["transaction_id"] == "T-DROP" and "boom" in failed["error"]
    # only the successful one is logged
    rec.assert_called_once_with(["T-DSD"], "netsuite")


def test_only_and_limit_filter_the_batch():
    out = push_invoices(INVOICES, live=False, refs=REFS_FULL, only=["T-DSD"])
    assert [r["transaction_id"] for r in out["results"]] == ["T-DSD"]
    out2 = push_invoices(INVOICES, live=False, refs=REFS_FULL, limit=1)
    assert len(out2["results"]) == 1


def test_limit_must_be_positive():
    """limit=0 used to fall through to 'no cap' and push everything; now rejected."""
    with pytest.raises(ValueError):
        push_invoices(INVOICES, live=False, refs=REFS_FULL, limit=0)
    with pytest.raises(ValueError):
        push_invoices(INVOICES, live=False, refs=REFS_FULL, limit=-1)
