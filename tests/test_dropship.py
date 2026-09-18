"""Dropship pre-fill (app.dropship): carrier + tracking onto the PACKED Finale
shipment, never a status and never a date -- shipping is the warehouse's end-of-day
click, which is what makes the actual ship date true. Strict: one live shipment and
one live label, or it reports rather than guesses."""
import pytest
from unittest.mock import patch

from app.dropship import live_shipments_by_po, plan_prefill, po_of, push_dropship_prefill

PUROLATOR = "/hddecorating/api/partygroup/100029"
SURL = "/hddecorating/api/shipment/100621"


def fship(po="538873472", status="SHIPMENT_PACKED", url=SURL, id_user="538873472-2"):
    return {"shipmentUrl": url, "shipmentIdUser": id_user, "statusId": status,
            "primaryOrderUrl": f"/hddecorating/api/order/{po}"}


def label(po="538873472", trk="520756038656", created="2026-09-17T12:45:20", voided=False):
    return {"shipmentId": 1, "orderNumber": po, "trackingNumber": trk, "createDate": created, "voided": voided}


class FakeFinale:
    def __init__(self, full=None, fail_update=False):
        self.calls = []
        self._full = full or {"shipmentUrl": SURL, "statusId": "SHIPMENT_PACKED", "trackingCode": None, "carrierPartyUrl": None}
        self.fail_update = fail_update
    def get_shipment(self, url): self.calls.append(("get", url)); return self._full
    def update_shipment(self, url, fields):
        if self.fail_update: raise RuntimeError("finale 500")
        self.calls.append(("update", url, fields)); return {"shipmentUrl": url, **fields}


def test_po_and_live_grouping_drops_cancelled():
    assert po_of(fship()) == "538873472"
    rows = [fship(), fship(status="SHIPMENT_CANCELLED", url="/s/1", id_user="538873472-1"), fship(po="OTHER", url="/s/2")]
    by_po = live_shipments_by_po(rows)
    assert [s["shipmentIdUser"] for s in by_po["538873472"]] == ["538873472-2"]      # the cancelled rebuild is ignored
    assert set(by_po) == {"538873472", "OTHER"}


def test_plan_writes_only_what_is_missing_and_never_ships():
    p = plan_prefill({"statusId": "SHIPMENT_PACKED", "trackingCode": None, "carrierPartyUrl": None}, "520756038656", PUROLATOR)
    assert p["action"] == "write" and p["fields"] == {"trackingCode": "520756038656", "carrierPartyUrl": PUROLATOR}
    assert "statusId" not in p["fields"] and "shipDate" not in p["fields"] and "shipDateEstimated" not in p["fields"]
    done = {"statusId": "SHIPMENT_PACKED", "trackingCode": "520756038656", "carrierPartyUrl": PUROLATOR}
    assert plan_prefill(done, "520756038656", PUROLATOR)["action"] == "equal"
    assert plan_prefill({**done, "carrierPartyUrl": None}, "520756038656", PUROLATOR)["fields"] == {"carrierPartyUrl": PUROLATOR}
    # the warehouse got there first: shipped is locked and its date is the one that counts
    assert plan_prefill({**done, "statusId": "SHIPMENT_SHIPPED", "trackingCode": None}, "520756038656", PUROLATOR)["action"] == "shipped"
    # carrier defaults off: tracking still goes on
    assert plan_prefill({"statusId": "SHIPMENT_INPUT"}, "520756038656", None)["fields"] == {"trackingCode": "520756038656"}


def test_live_writes_carrier_and_tracking_and_leaves_it_packed():
    f = FakeFinale()
    out = push_dropship_prefill([fship()], [label()], live=True, client=f, carrier_url=PUROLATOR)
    assert out["results"][0]["status"] == "prefilled" and out["summary"]["prefilled"] == 1
    assert f.calls[-1] == ("update", SURL, {"trackingCode": "520756038656", "carrierPartyUrl": PUROLATOR})
    dry = FakeFinale()
    out2 = push_dropship_prefill([fship()], [label()], live=False, client=dry, carrier_url=PUROLATOR)
    assert out2["results"][0]["status"] == "would_prefill" and "update" not in [c[0] for c in dry.calls]


def test_strict_reports_rather_than_guessing():
    two_labels = [label(), label(trk="520756000000", created="2026-09-17T13:00:00")]
    out = push_dropship_prefill([fship()], two_labels, live=True, client=FakeFinale(), carrier_url=PUROLATOR)
    assert out["results"][0]["status"] == "skipped_ambiguous" and "2 live labels" in out["results"][0]["error"]
    # a voided label does not count towards the ambiguity
    out2 = push_dropship_prefill([fship()], [label(), label(trk="x", voided=True)], live=True, client=FakeFinale(), carrier_url=PUROLATOR)
    assert out2["results"][0]["status"] == "prefilled"
    two_ships = [fship(), fship(url="/s/9", id_user="538873472-3")]
    out3 = push_dropship_prefill(two_ships, [label()], live=True, client=FakeFinale(), carrier_url=PUROLATOR)
    assert out3["results"][0]["status"] == "skipped_ambiguous" and "2 live Finale shipments" in out3["results"][0]["error"]


def test_waiting_shipped_floor_and_failure():
    out = push_dropship_prefill([], [label()], live=True, client=FakeFinale(), carrier_url=PUROLATOR)
    assert out["results"][0]["status"] == "skipped_no_shipment"            # no receipt: retried next poll
    out2 = push_dropship_prefill([fship(status="SHIPMENT_SHIPPED")], [label()], live=True, client=FakeFinale(), carrier_url=PUROLATOR)
    assert out2["results"][0]["status"] == "skipped_shipped"
    out3 = push_dropship_prefill([fship()], [label(created="2026-09-10T08:00:00")], live=True,
                                 client=FakeFinale(), carrier_url=PUROLATOR, created_after="2026-09-17")
    assert out3["results"][0]["status"] == "skipped_floor"
    out4 = push_dropship_prefill([fship()], [label()], live=True, client=FakeFinale(fail_update=True), carrier_url=PUROLATOR)
    assert out4["results"][0]["status"] == "failed" and "500" in out4["results"][0]["error"]


def test_cap_refuses_the_whole_run_and_only_narrows():
    rows = [fship(), fship(po="538879048", url="/s/2", id_user="538879048-1")]
    labels = [label(), label(po="538879048", trk="520756757388")]
    out = push_dropship_prefill(rows, labels, live=True, client=FakeFinale(), carrier_url=PUROLATOR, max_per_run=1)
    assert out["blocked"].startswith("2 shipments to write exceeds max_per_run 1")
    assert all(r["status"] == "would_prefill" for r in out["results"])
    f = FakeFinale()
    out2 = push_dropship_prefill(rows, labels, live=True, client=f, only=["538879048"], carrier_url=PUROLATOR)
    assert [r["po_number"] for r in out2["results"]] == ["538879048"]
    with pytest.raises(ValueError):
        push_dropship_prefill(rows, labels, live=False, limit=0)
