"""
Tests for the DSD pre-fill (app.finale_dsd + app.shipments.asn_shipping_refs): the ASN
reading rule, the per-shipment plan, the automation selection, and the push with a fake
Finale -- dry/live parity, the exact fields written, retry-vs-terminal receipts, and
failure isolation. No network.
"""
import pytest
from unittest.mock import patch

from app.finale_dsd import is_dsd_asn, plan_shipment, push_dsd_prefill, select_dsd_asns
from app.shipments import asn_shipping_refs

DSD_DETAIL = {"file": {"generic_json_edi": {"detail": {"shipments": [{
    "bill_of_lading_number": "3200416047", "carrier_reference_number": "6100994307",
    "carrier_details": {"identification_code": "HDOC", "routing_description": "6100994307"},
    "shipment_date": "2026-09-15"}]}}}}
DROP_DETAIL = {"file": {"generic_json_edi": {"detail": {"shipments": [{
    "carrier_details": {"routing_description": "UNSP"}, "shipped_date": "2026-09-15"}]}}}}

ASN = {"asn_id": "a1", "po_number": "40864264", "state": "Accepted", "created_at": "2026-09-16T10:00:00Z",
       "pro": "3200416047", "rts": "6100994307", "pickup_date": "2026-09-15"}
URL = "/hddecorating/api/shipment/100519"


def ship(status="SHIPMENT_PACKED", tracking=None, notes=None, url=URL, id_user="40864264-1"):
    return {"shipmentUrl": url, "shipmentIdUser": id_user, "statusId": status, "trackingCode": tracking, "publicNotes": notes}


class FakeFinale:
    account_id = "hddecorating"
    def __init__(self, order=True, shipments=(), fail_update=False):
        self.calls = []
        self._order = {"orderId": "40864264", "shipmentUrlList": [s["shipmentUrl"] for s in shipments]} if order else None
        self._ships = list(shipments)
        self.fail_update = fail_update
    def get_order(self, oid): self.calls.append(("get_order", oid)); return self._order
    def order_shipments(self, order): return self._ships
    def update_shipment(self, url, fields):
        if self.fail_update:
            raise RuntimeError("finale 500")
        self.calls.append(("update", url, fields)); return {"shipmentUrl": url, **fields}


def _run(client, live, asns=(ASN,), existing=None, **kw):
    with patch("app.tracking.get_finale_shipments", return_value=existing or {}), \
         patch("app.tracking.record_finale_shipment") as rec:
        out = push_dsd_prefill(list(asns), live=live, client=client, **kw)
    return out, rec


# ---------------------------------------------------------------- reading rule
def test_asn_shipping_refs_reads_pro_rts_pickup_from_a_dsd_856():
    assert asn_shipping_refs(DSD_DETAIL) == {"pro": "3200416047", "rts": "6100994307", "pickup_date": "2026-09-15"}
    assert is_dsd_asn({"pro": "3200416047"})


def test_dropship_856_has_no_pro_and_is_not_dsd():
    refs = asn_shipping_refs(DROP_DETAIL)
    assert refs["pro"] == "" and refs["pickup_date"] == ""
    assert not is_dsd_asn(refs)
    assert asn_shipping_refs({}) == {"pro": "", "rts": "", "pickup_date": ""}


# ---------------------------------------------------------------- per-shipment plan
@pytest.mark.parametrize("status", ["SHIPMENT_INPUT", "SHIPMENT_PACKED"])
def test_plan_writes_exactly_tracking_and_note_on_an_open_shipment(status):
    p = plan_shipment(ASN, ship(status))
    assert p["action"] == "write"
    assert p["fields"] == {"trackingCode": "3200416047", "publicNotes": "RTS 6100994307"}   # never shipDateEstimated


def test_plan_writes_only_what_is_missing():
    p = plan_shipment(ASN, ship(tracking="3200416047"))            # warehouse already typed the PRO
    assert p["fields"] == {"publicNotes": "RTS 6100994307"}
    # the warehouse's bare number counts as present (40864264-1 carries "6100994307")
    assert plan_shipment(ASN, ship("SHIPMENT_SHIPPED", tracking="3200416047", notes="6100994307"))["action"] == "equal"


def test_plan_equal_shipped_cancelled():
    assert plan_shipment(ASN, ship(tracking="3200416047", notes="RTS 6100994307"))["action"] == "equal"
    shipped = plan_shipment(ASN, ship("SHIPMENT_SHIPPED", tracking="3200416047", notes="RTS 6100994307"))
    assert shipped["action"] == "equal"                              # same numbers: nothing to do, even if shipped
    differs = plan_shipment(ASN, ship("SHIPMENT_SHIPPED", tracking="9999"))
    assert differs["action"] == "shipped" and "9999" in differs["reason"] and differs["fields"] == {}
    assert plan_shipment(ASN, ship("SHIPMENT_CANCELLED"))["action"] == "cancelled"


# ---------------------------------------------------------------- automation selection
def test_select_applies_accepted_floor_receipts_and_cap():
    states = {"a": {"state": "Accepted", "created_at": "2026-09-16T01:00:00Z"},
              "b": {"state": "Draft", "created_at": "2026-09-16T01:00:00Z"},
              "c": {"state": "Accepted", "created_at": "2026-09-10T01:00:00Z"},   # before the floor
              "d": {"state": "Accepted", "created_at": "2026-09-16T02:00:00Z"},
              "e": {"state": "Accepted", "created_at": "2026-09-16T03:00:00Z", "flavor": "Dropship"},
              "f": {"state": "Accepted", "created_at": "2026-09-16T04:00:00Z", "flavor": ""}}
    ids = select_dsd_asns(states, {"d"}, created_after="2026-09-15", created_within_days=None)
    assert ids == ["a", "f"]                                     # e: Dropship by flavor, never fetched; f: blank flavor = unknown, fetched


def test_cap_counts_shipments_about_to_be_written_not_pending_asns():
    a2 = {**ASN, "asn_id": "a2"}
    f = FakeFinale(shipments=[ship()])
    out, rec = _run(f, live=True, asns=[ASN, a2], max_per_run=1)
    assert out["blocked"].startswith("2 shipments to write exceeds max_per_run 1")
    assert not [c for c in f.calls if c[0] == "update"] and rec.assert_not_called() is None
    assert [r["status"] for r in out["results"]] == ["would_prefill", "would_prefill"]
    # a pending ASN (no shipment in Finale yet) does not count
    class Mixed(FakeFinale):
        def order_shipments(self, order): return self._ships if self.calls[-1][1] == "40864264" else []
    m = Mixed(shipments=[ship()])
    out2, _ = _run(m, live=True, asns=[ASN, {**a2, "po_number": "40864999"}], max_per_run=1)
    assert "blocked" not in out2 and [r["status"] for r in out2["results"]] == ["prefilled", "skipped_no_shipment"]


# ---------------------------------------------------------------- push
def test_dry_run_reports_would_prefill_and_writes_nothing():
    f = FakeFinale(shipments=[ship()])
    out, rec = _run(f, live=False)
    assert out["mode"] == "dry" and out["results"][0]["status"] == "would_prefill"
    assert out["results"][0]["shipments"][0]["fields"] == {"trackingCode": "3200416047", "publicNotes": "RTS 6100994307"}
    assert not [c for c in f.calls if c[0] == "update"] and rec.assert_not_called() is None


def test_live_writes_the_fields_and_records_a_receipt():
    f = FakeFinale(shipments=[ship()])
    out, rec = _run(f, live=True)
    r = out["results"][0]
    assert r["status"] == "prefilled" and r["shipment_id_user"] == "40864264-1"
    assert f.calls[-1] == ("update", URL, {"trackingCode": "3200416047", "publicNotes": "RTS 6100994307"})
    rec.assert_called_once_with("a1", "40864264", "40864264-1", "3200416047", "6100994307", "prefilled")
    assert out["summary"]["prefilled"] == 1


def test_missing_order_or_shipment_is_retried_not_receipted():
    out, rec = _run(FakeFinale(order=False), live=True)
    assert out["results"][0]["status"] == "skipped_no_order" and "retry" in out["results"][0]["error"]
    out2, rec2 = _run(FakeFinale(shipments=[ship("SHIPMENT_CANCELLED")]), live=True)
    assert out2["results"][0]["status"] == "skipped_no_shipment"
    rec.assert_not_called(); rec2.assert_not_called()


def test_equal_and_shipped_are_terminal_with_receipts_in_live_only():
    done = ship(tracking="3200416047", notes="RTS 6100994307")
    out, rec = _run(FakeFinale(shipments=[done]), live=True)
    assert out["results"][0]["status"] == "skipped_equal"
    assert rec.call_args[0][-1] == "skipped_equal"
    out, rec = _run(FakeFinale(shipments=[done]), live=False)
    assert out["results"][0]["status"] == "skipped_equal" and rec.assert_not_called() is None
    gone = ship("SHIPMENT_SHIPPED", tracking="9999")
    f = FakeFinale(shipments=[gone])
    out, rec = _run(f, live=True)
    assert out["results"][0]["status"] == "skipped_shipped" and "9999" in out["results"][0]["error"]
    assert not [c for c in f.calls if c[0] == "update"] and rec.call_args[0][-1] == "skipped_shipped"


def test_prior_receipt_not_accepted_and_not_dsd_are_skipped_before_finale():
    f = FakeFinale(shipments=[ship()])
    out, _ = _run(f, live=True, existing={"a1": {"status": "prefilled", "shipment_id": "40864264-1"}})
    assert out["results"][0]["status"] == "skipped_done"
    out, _ = _run(f, live=True, asns=[{**ASN, "state": "Draft"}])
    assert out["results"][0]["status"] == "skipped_not_accepted"
    out, rec = _run(f, live=True, asns=[{**ASN, "pro": ""}])
    assert out["results"][0]["status"] == "skipped_not_dsd"
    rec.assert_called_once_with("a1", "40864264", None, None, None, "skipped_not_dsd")   # terminal: never re-read
    assert f.calls == []


def test_one_failure_does_not_stop_the_batch_and_only_limit_filter():
    bad = FakeFinale(shipments=[ship()], fail_update=True)
    out, rec = _run(bad, live=True, asns=[ASN, {**ASN, "asn_id": "a2"}])
    assert [r["status"] for r in out["results"]] == ["failed", "failed"] and out["summary"]["failed"] == 2
    rec.assert_not_called()
    out, _ = _run(FakeFinale(shipments=[ship()]), live=False, asns=[ASN, {**ASN, "asn_id": "a2"}], only=["a2"])
    assert [r["asn_id"] for r in out["results"]] == ["a2"]
    out, _ = _run(FakeFinale(shipments=[ship()]), live=False, asns=[ASN, {**ASN, "asn_id": "a2"}], limit=1)
    assert [r["asn_id"] for r in out["results"]] == ["a1"]
    with pytest.raises(ValueError):
        push_dsd_prefill([ASN], live=False, client=FakeFinale(), limit=0)
