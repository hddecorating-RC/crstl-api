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
    "amount_guards": {"dsd": {"floor": 1, "ceiling": 100000},
                      "dropship": {"floor": 1, "ceiling": 100000}},
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
    def get_by_external_id(self, record_type, external_id):
        return None
    def upsert(self, payload, record_type="invoice", guard_last_modified=None):
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
    assert out["summary"] == {"built": 2, "sent": 0, "failed": 0, "skipped_no_map": 1, "skipped_modified": 0, "skipped_invalid": 0, "skipped_no_baseline": 0, "skipped_exists": 0, "skipped_conflict": 0}
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
    assert out["summary"] == {"built": 2, "sent": 2, "failed": 0, "skipped_no_map": 1, "skipped_modified": 0, "skipped_invalid": 0, "skipped_no_baseline": 0, "skipped_exists": 0, "skipped_conflict": 0}
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
    assert out["summary"] == {"built": 2, "sent": 1, "failed": 1, "skipped_no_map": 1, "skipped_modified": 0, "skipped_invalid": 0, "skipped_no_baseline": 0, "skipped_exists": 0, "skipped_conflict": 0}
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
        def get_by_external_id(self, record_type, external_id):
            return None
        def upsert(self, payload, record_type="invoice", guard_last_modified=None):
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
        def get_by_external_id(self, record_type, external_id):
            return None
        def upsert(self, payload, record_type="invoice", guard_last_modified=None):
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


def test_amount_flag_is_advisory_not_blocking():
    """H3: an out-of-range gross is FLAGGED on the row but still built/pushed --
    soft guard, never blocks."""
    big = {**INVOICES[1], "transaction_id": "T-BIG", "source_document_id": "S-BIG",
           "subtotal": 999999.0, "total_amount": 999999.0}   # dropship, way over ceiling
    out = push_invoices([big], live=False, refs=REFS_FULL)
    r = out["results"][0]
    assert r["status"] == "built"              # NOT blocked
    assert r["amount_flag"] == "above_ceiling"


def test_push_builds_sales_order_when_configured():
    """record_type=salesOrder in refs -> the engine builds an SO body (orderStatus,
    no invoice-only fields) and hands the client record_type='salesOrder', so a
    config flip is all it takes to switch what the connector creates."""
    refs_so = {**REFS_FULL, "record_type": "salesOrder",
               "sales_order": {"order_status_id": "A", "custom_form_id": "231"}}

    class RecordingClient:
        def __init__(self):
            self.calls = []
        def get_by_external_id(self, record_type, external_id):
            return None
        def upsert(self, payload, record_type="invoice", guard_last_modified=None):
            self.calls.append((record_type, payload))
            return {"location": "/x", "action": "created", "netsuite_id": "1", "last_modified": "T"}

    client = RecordingClient()
    with patch("app.tracking.record_events"), \
         patch("app.tracking.get_netsuite_last_modified", return_value="OLD"), \
         patch("app.tracking.record_netsuite_push"):
        out = push_invoices(INVOICES, live=True, refs=refs_so, client=client)
    assert out["summary"]["sent"] == 2
    assert client.calls and all(rt == "salesOrder" for rt, _ in client.calls)
    p = client.calls[0][1]
    assert p["orderStatus"] == {"id": "A"}          # Pending Approval carried through
    assert p["customForm"] == {"id": "231"}
    assert p["custbodyinvoicepercent"] == 100        # INVOICE % forced to 100 (form default is 50)


def _dt(s):
    from datetime import datetime, timezone
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_select_for_automation_floor_keys_on_created_at_not_invoice_date():
    """The floor guard drops rows CREATED before created_after (and undated ones),
    keeps the floor date itself (inclusive), and only pushes the not-yet-pushed set.
    It reads created_at, NOT invoice_date -- proven by the garbage-invoice_date row,
    which is kept because CRSTL created it after the floor. Manual pushes never call
    this. `now` far in the future so the rolling window doesn't bind here."""
    from app.netsuite_push import select_for_automation
    cand = [
        {"transaction_id": "old",     "created_at": "2026-03-10T09:00:00Z"},  # pre-floor -> drop
        {"transaction_id": "day0",    "created_at": "2026-09-11T00:30:00Z"},  # floor day  -> keep
        {"transaction_id": "new",     "created_at": "2026-09-14T20:00:00Z"},  # after      -> keep
        {"transaction_id": "nocreat", "created_at": ""},                      # undated    -> drop
        {"transaction_id": "garbage", "created_at": "2026-09-12T10:00:00Z",   # bogus invoice_date
                                      "invoice_date": "2008-11-10"},          #   -> keep (by created_at)
        {"transaction_id": "pushed",  "created_at": "2026-09-13T10:00:00Z"},  # already pushed -> drop
    ]
    unpushed = {"old", "day0", "new", "nocreat", "garbage"}   # 'pushed' not in unpushed
    to_push, blocked = select_for_automation(
        cand, unpushed, created_after="2026-09-11", max_per_run=75, now=_dt("2026-12-01"))
    assert blocked is None
    assert {i["transaction_id"] for i in to_push} == {"day0", "new", "garbage"}


def test_select_for_automation_rolling_window_excludes_old_backlog():
    """The created_within_days window is the self-scaling guard: on a given run it
    admits only recent inflow, so an Accepted-but-never-pushed backlog created weeks
    ago is never dredged up -- even though the fixed floor would still admit it. The
    effective lower bound is the LATER of created_after and (now - N days)."""
    from app.netsuite_push import select_for_automation
    cand = [
        {"transaction_id": "backlog", "created_at": "2026-09-11T10:00:00Z"},  # > floor, but old
        {"transaction_id": "recent",  "created_at": "2026-09-24T10:00:00Z"},  # within 7d of now
    ]
    unpushed = {"backlog", "recent"}
    to_push, blocked = select_for_automation(
        cand, unpushed, created_after="2026-09-11", created_within_days=7,
        max_per_run=75, now=_dt("2026-09-25"))
    assert blocked is None
    assert {i["transaction_id"] for i in to_push} == {"recent"}   # backlog aged out


def test_select_for_automation_floor_beats_window_when_window_is_wider():
    """A window wider than the time since go-live must not relax the floor below
    created_after -- the pre-go-live backlog stays out no matter how wide N is."""
    from app.netsuite_push import select_for_automation
    cand = [
        {"transaction_id": "pre",  "created_at": "2026-09-05T10:00:00Z"},  # before floor
        {"transaction_id": "post", "created_at": "2026-09-12T10:00:00Z"},  # after floor
    ]
    unpushed = {"pre", "post"}
    to_push, blocked = select_for_automation(
        cand, unpushed, created_after="2026-09-11", created_within_days=365,
        max_per_run=75, now=_dt("2026-09-13"))
    assert blocked is None
    assert {i["transaction_id"] for i in to_push} == {"post"}


def test_select_for_automation_refuses_over_cap():
    """A run larger than max_per_run is REFUSED whole (not truncated), so a scope
    slip can't blast -- a human reviews and runs it manually."""
    from app.netsuite_push import select_for_automation
    cand = [{"transaction_id": str(n), "created_at": "2026-09-14T10:00:00Z"} for n in range(10)]
    unpushed = {str(n) for n in range(10)}
    to_push, blocked = select_for_automation(
        cand, unpushed, created_after="2026-09-11", max_per_run=5, now=_dt("2026-09-15"))
    assert to_push == []
    assert "exceeds max_per_run 5" in blocked


def test_select_for_automation_no_guards_is_passthrough():
    from app.netsuite_push import select_for_automation
    cand = [{"transaction_id": "a", "created_at": "2026-01-01T10:00:00Z"}]
    to_push, blocked = select_for_automation(cand, {"a"})   # no floor, no window, no cap
    assert blocked is None and [i["transaction_id"] for i in to_push] == ["a"]


def test_reconcile_flag_none_when_total_matches_crstl():
    """Row ties to CRSTL's 810 total (delta 0) -> no reconcile flag."""
    inv = {"transaction_id": "T-OK", "source_document_id": "S-OK", "invoice_number": "INVOK",
           "product": "Drape Panel", "status": "Accepted", "po_number": "POK",
           "invoice_date": "2026-09-11", "due_date": "", "subtotal": 100.0,
           "allowance_amount": 6.0, "discount_amount": 0.0, "total_amount": 106.22,
           "store": "VAUGHAN", "province": "ON"}
    r = push_invoices([inv], live=False, refs=REFS_FULL)["results"][0]
    assert r["status"] == "built" and r["total"] == 106.22 and r["reconcile_flag"] is None


def test_reconcile_flag_set_when_total_differs_from_crstl():
    """A total that doesn't match CRSTL's 810 is flagged, not silently booked."""
    inv = {"transaction_id": "T-BAD", "source_document_id": "S-BAD", "invoice_number": "INVBAD",
           "product": "Drape Panel", "status": "Accepted", "po_number": "POB",
           "invoice_date": "2026-09-11", "due_date": "", "subtotal": 100.0,
           "allowance_amount": 6.0, "discount_amount": 0.0, "total_amount": 200.0,
           "store": "VAUGHAN", "province": "ON"}
    r = push_invoices([inv], live=False, refs=REFS_FULL)["results"][0]
    assert r["reconcile_flag"] and "off CRSTL 810" in r["reconcile_flag"]


def test_skips_existing_record_without_confirm():
    """A record already in NetSuite is SKIPPED (never overwritten) unless confirmed."""
    class ExistsClient:
        def __init__(self): self.upserts = []
        def get_by_external_id(self, record_type, external_id):
            return {"id": "999", "tranId": "SO999", "lastModifiedDate": "T1"}
        def upsert(self, payload, record_type="invoice", guard_last_modified=None):
            self.upserts.append(payload)
            return {"location": "/x", "action": "updated", "netsuite_id": "999", "last_modified": "T2"}
    client = ExistsClient()
    with patch("app.tracking.record_events"), patch("app.tracking.record_netsuite_push"):
        out = push_invoices(INVOICES, live=True, refs=REFS_FULL, client=client)   # confirm_existing default False
    assert client.upserts == []                                    # nothing written
    statuses = {r["status"] for r in out["results"] if r["status"] != "skipped_no_map"}
    assert statuses == {"skipped_exists"}
    assert out["summary"]["skipped_exists"] == 2 and out["summary"]["sent"] == 0


def test_confirm_existing_updates_the_record():
    """With confirm_existing=True the existing record is UPDATED, guarded by its
    fresh lastModifiedDate."""
    class ExistsClient:
        def __init__(self): self.guards = []
        def get_by_external_id(self, record_type, external_id):
            return {"id": "999", "tranId": "SO999", "lastModifiedDate": "T1"}
        def upsert(self, payload, record_type="invoice", guard_last_modified=None):
            self.guards.append(guard_last_modified)
            return {"location": "/x", "action": "updated", "netsuite_id": "999", "last_modified": "T2"}
    client = ExistsClient()
    with patch("app.tracking.record_events"), patch("app.tracking.record_netsuite_push"):
        out = push_invoices(INVOICES, live=True, refs=REFS_FULL, client=client, confirm_existing=True)
    assert out["summary"]["sent"] == 2 and out["summary"]["skipped_exists"] == 0
    assert all(g == "T1" for g in client.guards)                   # fresh lastModified used as guard


def test_skips_on_external_id_type_conflict():
    """An externalId held by a DIFFERENT transaction type (an invoice) -> skipped_conflict,
    never a create attempt."""
    from app.netsuite_client import NetSuiteExternalIdConflict
    class ConflictClient:
        def __init__(self): self.upserts = []
        def get_by_external_id(self, record_type, external_id):
            raise NetSuiteExternalIdConflict("held by an invoice")
        def upsert(self, payload, record_type="invoice", guard_last_modified=None):
            self.upserts.append(payload); return {}
    client = ConflictClient()
    with patch("app.tracking.record_events"), patch("app.tracking.record_netsuite_push"):
        out = push_invoices(INVOICES, live=True, refs=REFS_FULL, client=client)
    assert client.upserts == []
    assert out["summary"]["skipped_conflict"] == 2 and out["summary"]["sent"] == 0


def test_reconcile_flag_ignores_penny_rounding():
    """A <=1c delta (compound-tax component rounding: QC GST+QST) is NOT
    flagged; only structural (>1c) mismatches are."""
    inv = {"transaction_id": "T-1C", "source_document_id": "S-1C", "invoice_number": "INV1C",
           "product": "Drape Panel", "status": "Accepted", "po_number": "PO1C",
           "invoice_date": "2026-09-11", "due_date": "", "subtotal": 100.0,
           "allowance_amount": 6.0, "discount_amount": 0.0, "total_amount": 106.23,  # 1c over our 106.22
           "store": "VAUGHAN", "province": "ON"}
    r = push_invoices([inv], live=False, refs=REFS_FULL)["results"][0]
    assert round(r["delta"], 2) == -0.01 and r["reconcile_flag"] is None
