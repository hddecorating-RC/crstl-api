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
    def __init__(self, full=None, fail_update=False, order_status="ORDER_LOCKED"):
        self.calls = []
        self._full = full or {"shipmentUrl": SURL, "statusId": "SHIPMENT_PACKED", "trackingCode": None, "carrierPartyUrl": None}
        self.fail_update = fail_update
        self._order_status = order_status
    def get_shipment(self, url): self.calls.append(("get", url)); return self._full
    def get_order(self, po): return {"orderId": po, "statusId": self._order_status}
    def reopen_order(self, order):
        self.calls.append(("reopen", order.get("orderId")))
        self._order_status = "ORDER_LOCKED"; return {**order, "statusId": "ORDER_LOCKED"}
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


def test_a_closed_order_is_reopened_so_the_warehouse_can_ship_it():
    """The connection still completes the order on the ship event (verified on
    TEST_0007, 2026-09-17), and a closed order is neither writable by us nor
    shippable by the warehouse. Reopen first, and do it even when the shipment
    already has everything -- the reopen is the point."""
    f = FakeFinale(order_status="ORDER_COMPLETED")
    out = push_dropship_prefill([fship()], [label()], live=True, client=f, carrier_url=PUROLATOR)
    r = out["results"][0]
    assert r["status"] == "prefilled" and r["reopened"] is True
    assert [c[0] for c in f.calls] == ["get", "reopen", "update"]          # reopen BEFORE the write
    # already open: no reopen
    open_ = FakeFinale(order_status="ORDER_LOCKED")
    out2 = push_dropship_prefill([fship()], [label()], live=True, client=open_, carrier_url=PUROLATOR)
    assert "reopened" not in out2["results"][0] and "reopen" not in [c[0] for c in open_.calls]
    # nothing to write, but the order is closed: still reopened, so the warehouse can ship
    done = FakeFinale(full={"shipmentUrl": SURL, "statusId": "SHIPMENT_PACKED",
                            "trackingCode": "520756038656", "carrierPartyUrl": PUROLATOR},
                      order_status="ORDER_COMPLETED")
    out3 = push_dropship_prefill([fship()], [label()], live=True, client=done, carrier_url=PUROLATOR)
    assert out3["results"][0]["status"] == "skipped_equal" and out3["results"][0]["reopened"] is True
    assert ("reopen", "538873472") in done.calls
    # dry run reports the reopen and performs none
    dry = FakeFinale(order_status="ORDER_COMPLETED")
    out4 = push_dropship_prefill([fship()], [label()], live=False, client=dry, carrier_url=PUROLATOR)
    assert out4["results"][0]["reopened"] is True and "reopen" not in [c[0] for c in dry.calls]


# ── fewer Finale reads (2026-09-21: Monday's 36 labels ran the poll into 429s) ──

class CountingFinale(FakeFinale):
    """FakeFinale that also records order reads."""
    def get_order(self, po):
        self.calls.append(("order", po)); return super().get_order(po)


FILLED = ("520756038656", PUROLATOR)


def test_a_shipment_filled_in_earlier_is_not_read_again_while_its_order_is_open():
    f = CountingFinale()
    out = push_dropship_prefill([fship()], [label()], live=True, client=f, carrier_url=PUROLATOR,
                                order_status={"538873472": "ORDER_LOCKED"}, marks={SURL: FILLED})
    r = out["results"][0]
    assert r["status"] == "skipped_equal" and "checked earlier" in r["error"]
    assert f.calls == [] and "mark" not in r                        # no reads, nothing new to record


def test_a_remembered_shipment_whose_order_was_closed_since_is_still_reopened():
    """The connection closes the order seconds to minutes after the label -- possibly
    after we filled the shipment in. The listing shows it closed: full treatment."""
    f = CountingFinale(full={"shipmentUrl": SURL, "statusId": "SHIPMENT_PACKED",
                             "trackingCode": FILLED[0], "carrierPartyUrl": PUROLATOR},
                       order_status="ORDER_COMPLETED")
    out = push_dropship_prefill([fship()], [label()], live=True, client=f, carrier_url=PUROLATOR,
                                order_status={"538873472": "ORDER_COMPLETED"}, marks={SURL: FILLED})
    r = out["results"][0]
    assert r["status"] == "skipped_equal" and r["reopened"] is True
    assert [c[0] for c in f.calls] == ["get", "order", "reopen"]


def test_a_changed_label_or_carrier_means_a_fresh_read():
    for mark in (("520700000000", PUROLATOR), (FILLED[0], "/hddecorating/api/partygroup/1")):
        f = CountingFinale()
        push_dropship_prefill([fship()], [label()], live=True, client=f, carrier_url=PUROLATOR,
                              order_status={"538873472": "ORDER_LOCKED"}, marks={SURL: mark})
        assert ("get", SURL) in f.calls


def test_an_open_order_in_the_listing_is_not_read_one_by_one():
    f = CountingFinale()
    out = push_dropship_prefill([fship()], [label()], live=True, client=f, carrier_url=PUROLATOR,
                                order_status={"538873472": "ORDER_LOCKED"}, marks={})
    assert out["results"][0]["status"] == "prefilled"
    assert [c[0] for c in f.calls] == ["get", "update"]              # no ("order", ...)
    # a PO the listing does not know: read it, as before
    g = CountingFinale()
    push_dropship_prefill([fship()], [label()], live=True, client=g, carrier_url=PUROLATOR, order_status={}, marks={})
    assert ("order", "538873472") in g.calls


def test_rows_carry_a_mark_only_for_what_this_run_established():
    wrote = push_dropship_prefill([fship()], [label()], live=True, client=CountingFinale(), carrier_url=PUROLATOR)
    assert wrote["results"][0]["mark"] == {"shipment_url": SURL, "po_number": "538873472",
                                           "tracking": FILLED[0], "carrier_url": PUROLATOR}
    already = CountingFinale(full={"shipmentUrl": SURL, "statusId": "SHIPMENT_PACKED",
                                   "trackingCode": FILLED[0], "carrierPartyUrl": PUROLATOR})
    assert push_dropship_prefill([fship()], [label()], live=True, client=already,
                                 carrier_url=PUROLATOR)["results"][0]["mark"]["tracking"] == FILLED[0]
    dry = push_dropship_prefill([fship()], [label()], live=False, client=CountingFinale(), carrier_url=PUROLATOR)
    assert "mark" not in dry["results"][0]                             # would_prefill: nothing on it yet
    failed = push_dropship_prefill([fship()], [label()], live=True, client=CountingFinale(fail_update=True),
                                   carrier_url=PUROLATOR)
    assert "mark" not in failed["results"][0]


def test_the_runner_remembers_live_results_and_survives_a_failed_order_listing(monkeypatch):
    from app import finale_jobs, tracking
    fin = CountingFinale()
    fin.carrier_index = lambda: {"Purolator Canada": PUROLATOR}
    fin.list_shipments = lambda: [fship()]
    fin.list_sale_orders = lambda: [{"orderId": "538873472", "statusId": "ORDER_LOCKED"}]
    ss = type("SS", (), {"list_shipments": lambda self, store, since: [label()]})
    monkeypatch.setattr("app.finale_jobs._dropship_config", lambda: {"enabled": True, "store_id": 1})
    monkeypatch.setattr("app.finale_jobs._finale_config",
                        lambda: {"carriers": {"enabled": True, "dropship": "Purolator Canada"}})
    with patch("app.finale.FinaleClient", return_value=fin) as FC, patch("app.finale_jobs.ShipStationClient") as SC:
        FC.configured.return_value = True
        SC.configured.return_value = True
        SC.return_value = ss()
        finale_jobs._run_dropship_prefill(False, ["538873472"], None)
        assert tracking.get_dropship_marks() == {}                        # a dry run remembers nothing
        finale_jobs._run_dropship_prefill(True, ["538873472"], None)
        assert tracking.get_dropship_marks() == {SURL: FILLED}
        fin.calls.clear()
        again = finale_jobs._run_dropship_prefill(True, ["538873472"], None)
        assert again["results"][0]["status"] == "skipped_equal" and fin.calls == []
        def boom():
            raise RuntimeError("429")
        fin.list_sale_orders = boom                                        # listing down: per-order reads
        finale_jobs._run_dropship_prefill(False, ["538873472"], None)
        assert ("order", "538873472") in fin.calls
