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
    {"transaction_id": "T-DSD", "source_document_id": "S-DSD", "invoice_number": "INV1", "product": "Drape Panel", "status": "Accepted", "po_number": "PO1",
     "invoice_date": "2026-09-01", "due_date": "2026-10-01", "subtotal": 100.00,
     "total_amount": 106.00, "store": "VAUGHAN", "province": "ON"},
    {"transaction_id": "T-DROP", "source_document_id": "S-DROP", "invoice_number": "INV2", "product": "Drape Panel", "status": "Accepted", "po_number": "PO2",
     "invoice_date": "2026-09-01", "due_date": "2026-10-01", "subtotal": 200.00,
     "total_amount": 210.00, "store": None, "province": "ON"},
    {"transaction_id": "T-NOMAP", "source_document_id": "S-NOMAP", "invoice_number": "INV3", "product": "Drape Panel", "status": "Accepted", "po_number": "PO3",
     "invoice_date": "2026-09-01", "due_date": "", "subtotal": 50.00,
     "total_amount": 50.00, "store": None, "province": "XX"},
]


class FakeClient:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on or set()
    def upsert_invoice(self, payload, guard_last_modified=None):
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
    assert out["summary"] == {"built": 2, "sent": 0, "failed": 0, "skipped_no_map": 1, "skipped_modified": 0, "skipped_invalid": 0, "skipped_no_baseline": 0}
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
    with patch("app.tracking.record_events") as rec, \
         patch("app.tracking.get_netsuite_last_modified", return_value=None), \
         patch("app.tracking.record_netsuite_push"):
        out = push_invoices(INVOICES, live=True, refs=REFS_FULL, client=client)
    assert out["mode"] == "live"
    assert out["summary"] == {"built": 2, "sent": 2, "failed": 0, "skipped_no_map": 1, "skipped_modified": 0, "skipped_invalid": 0, "skipped_no_baseline": 0}
    assert len(client.calls) == 2
    sent = [r for r in out["results"] if r["status"] == "sent"]
    assert {r["transaction_id"] for r in sent} == {"T-DSD", "T-DROP"}
    assert sent[0]["location"].startswith("/record/v1/invoice/")
    rec.assert_called_once_with(["T-DSD", "T-DROP"], "netsuite")


def test_live_one_failure_does_not_stop_the_batch():
    client = FakeClient(fail_on={"CRSTL-S-DROP"})  # payload externalId = CRSTL-<source_document_id>
    with patch("app.tracking.record_events") as rec, \
         patch("app.tracking.get_netsuite_last_modified", return_value=None), \
         patch("app.tracking.record_netsuite_push"):
        out = push_invoices(INVOICES, live=True, refs=REFS_FULL, client=client)
    assert out["summary"] == {"built": 2, "sent": 1, "failed": 1, "skipped_no_map": 1, "skipped_modified": 0, "skipped_invalid": 0, "skipped_no_baseline": 0}
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


def test_select_latest_accepted_dedups_by_source_doc():
    from app.netsuite_push import select_latest_accepted
    rows = [
        {"source_document_id": "S1", "status": "Accepted", "invoice_date": "2026-09-01", "transaction_id": "a"},
        {"source_document_id": "S1", "status": "Accepted", "invoice_date": "2026-09-05", "transaction_id": "b"},  # latest accepted
        {"source_document_id": "S1", "status": "Draft",    "invoice_date": "2026-09-09", "transaction_id": "c"},  # draft ignored, even if newer
        {"source_document_id": "S2", "status": "Draft",    "invoice_date": "2026-09-01", "transaction_id": "d"},  # no accepted -> dropped
        {"source_document_id": "",   "status": "Accepted", "invoice_date": "2026-09-01", "transaction_id": "e"},  # no source doc -> dropped
    ]
    out = select_latest_accepted(rows)
    assert {r["source_document_id"] for r in out} == {"S1"}
    assert len(out) == 1 and out[0]["transaction_id"] == "b"


def test_modified_on_server_is_skipped_not_overwritten():
    """OMIS's optimistic-lock guard: if NetSuite raises modified-on-server for one
    invoice, that one is SKIPPED (not overwritten) and the batch continues."""
    from app.netsuite_client import NetSuiteModifiedOnServer

    class GuardClient:
        def __init__(self):
            self.calls = []
        def upsert_invoice(self, payload, guard_last_modified=None):
            self.calls.append(payload["externalId"])
            if payload["externalId"] == "CRSTL-S-DROP":
                raise NetSuiteModifiedOnServer("changed on server")
            return {"location": "/x", "action": "created", "netsuite_id": "1", "last_modified": "T1"}

    client = GuardClient()
    with patch("app.tracking.record_events") as rec, \
         patch("app.tracking.get_netsuite_last_modified", return_value="OLD"), \
         patch("app.tracking.record_netsuite_push"):
        out = push_invoices(INVOICES, live=True, refs=REFS_FULL, client=client)
    st = {r["transaction_id"]: r["status"] for r in out["results"]}
    assert st["T-DROP"] == "skipped_modified"    # not overwritten
    assert st["T-DSD"] == "sent"
    assert out["summary"]["skipped_modified"] == 1
    assert out["summary"]["sent"] == 1
    rec.assert_called_once_with(["T-DSD"], "netsuite")   # only the sent one logged


def test_push_invoices_enforces_eligibility_itself():
    """M1 regression: the Accepted/latest/non-zero filter lives INSIDE
    push_invoices, so the CLI (which calls it directly) cannot push a Draft, a
    stale version, or a zero-value row by going around _run_netsuite_push."""
    rows = [
        dict(INVOICES[0]),  # T-DSD, Accepted, non-zero -> eligible
        {"transaction_id": "T-DRAFT", "source_document_id": "S-DRAFT", "status": "Draft",
         "invoice_number": "INVd", "product": "Drape Panel", "po_number": "PO9",
         "invoice_date": "2026-09-01", "due_date": "", "subtotal": 500.0,
         "total_amount": 500.0, "store": "VAUGHAN", "province": "ON"},
        {"transaction_id": "T-ZERO", "source_document_id": "S-ZERO", "status": "Accepted",
         "invoice_number": "INVz", "product": "Drape Panel", "po_number": "PO8",
         "invoice_date": "2026-09-01", "due_date": "", "subtotal": 0.0,
         "total_amount": 0.0, "store": "VAUGHAN", "province": "ON"},
    ]
    out = push_invoices(rows, live=False, refs=REFS_FULL)
    tids = {r["transaction_id"] for r in out["results"]}
    assert "T-DSD" in tids            # eligible
    assert "T-DRAFT" not in tids      # Draft filtered by the engine
    assert "T-ZERO" not in tids       # zero-value filtered by the engine


def test_push_invoices_keeps_latest_accepted_per_source_doc():
    """Two Accepted versions of one logical invoice -> only the latest is pushed,
    even calling push_invoices directly (the CLI path)."""
    base = {"store": "VAUGHAN", "province": "ON", "product": "Drape Panel",
            "status": "Accepted", "subtotal": 100.0, "total_amount": 106.0,
            "po_number": "PO1", "due_date": ""}
    rows = [
        {**base, "transaction_id": "v1", "source_document_id": "SAME",
         "invoice_number": "INVx", "invoice_date": "2026-09-01"},
        {**base, "transaction_id": "v2", "source_document_id": "SAME",
         "invoice_number": "INVx", "invoice_date": "2026-09-05"},   # latest
    ]
    out = push_invoices(rows, live=False, refs=REFS_FULL)
    tids = [r["transaction_id"] for r in out["results"]]
    assert tids == ["v2"]   # one record, the latest version


def test_push_invoices_skips_unsafe_source_document_id():
    """H2/L2: an invoice whose source_document_id carries URL metacharacters is
    skipped (skipped_invalid) and the batch continues -- it never reaches the
    NetSuite URL."""
    bad = {**INVOICES[0], "transaction_id": "T-BAD", "source_document_id": "x?replace=none&"}
    out = push_invoices([dict(INVOICES[0]), bad], live=False, refs=REFS_FULL)
    st = {r["transaction_id"]: r["status"] for r in out["results"]}
    assert st["T-BAD"] == "skipped_invalid"
    assert st["T-DSD"] == "built"
    assert out["summary"]["skipped_invalid"] == 1


def test_push_invoices_skips_no_baseline_never_overwrites():
    """M2: if the client reports a record exists with no baseline, the engine skips
    it (skipped_no_baseline) and continues -- it never overwrites."""
    from app.netsuite_client import NetSuiteNoBaseline

    class NoBaseClient:
        def __init__(self):
            self.calls = []
        def upsert_invoice(self, payload, guard_last_modified=None):
            self.calls.append(payload["externalId"])
            if payload["externalId"] == "CRSTL-S-DROP":
                raise NetSuiteNoBaseline("no baseline for this record")
            return {"location": "/x", "action": "created", "netsuite_id": "1", "last_modified": "T"}

    client = NoBaseClient()
    with patch("app.tracking.record_events") as rec, \
         patch("app.tracking.get_netsuite_last_modified", return_value=None), \
         patch("app.tracking.record_netsuite_push"):
        out = push_invoices(INVOICES, live=True, refs=REFS_FULL, client=client)
    st = {r["transaction_id"]: r["status"] for r in out["results"]}
    assert st["T-DROP"] == "skipped_no_baseline"    # not overwritten
    assert st["T-DSD"] == "sent"
    assert out["summary"]["skipped_no_baseline"] == 1
    rec.assert_called_once_with(["T-DSD"], "netsuite")
