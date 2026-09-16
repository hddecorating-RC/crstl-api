"""ShipStation DSD close (app.shipstation): Finale-shipped DSD orders are marked
shipped in ShipStation with the PRO and Finale's ship date, never notified, never
before the floor, never when cancelled/unshipped in Finale, once per order, capped."""
import pytest
from unittest.mock import MagicMock, patch

from app.shipstation import ShipStationClient, plan_close, push_shipstation_close

SS = {"orderId": 323160921, "orderNumber": "40842996", "orderStatus": "awaiting_shipment", "createDate": "2026-09-08T16:02:11.0000000"}
SHIPPED = [{"shipmentIdUser": "40842996-1", "statusId": "SHIPMENT_SHIPPED", "trackingCode": "6100994307", "shipDate": "2026-09-16T16:00:00"}]
PACKED = [{"shipmentIdUser": "40842996-1", "statusId": "SHIPMENT_PACKED", "trackingCode": "6100994307", "shipDate": None}]
FIN = {"orderId": "40842996", "statusId": "ORDER_COMPLETED", "shipmentUrlList": ["/s/1"]}


def test_plan_close_rules():
    p = plan_close(SS, FIN, SHIPPED, created_after="2026-09-01")
    assert p["action"] == "close" and p["fields"] == {"carrierCode": "other", "trackingNumber": "6100994307", "shipDate": "2026-09-16"}
    assert plan_close(SS, FIN, PACKED, created_after="2026-09-01")["action"] == "skipped_not_shipped"
    assert plan_close(SS, None, [], created_after="2026-09-01")["action"] == "skipped_no_finale"
    assert plan_close(SS, {**FIN, "statusId": "ORDER_CANCELLED"}, SHIPPED, created_after="2026-09-01")["action"] == "skipped_cancelled"
    assert plan_close({**SS, "createDate": "2026-08-25T10:00:00"}, FIN, SHIPPED, created_after="2026-09-01")["action"] == "skipped_floor"
    assert plan_close({**SS, "orderStatus": "shipped"}, FIN, SHIPPED, created_after=None)["action"] == "skipped_status"
    assert plan_close({**SS, "orderStatus": "awaiting_payment"}, FIN, SHIPPED, created_after=None)["action"] == "close"   # payment status is noise
    # two moved shipments: the LAST ship date and its tracking
    two = SHIPPED + [{"statusId": "SHIPMENT_DELIVERED", "trackingCode": "6100999999", "shipDate": "2026-09-17T16:00:00"}]
    assert plan_close(SS, FIN, two, created_after=None)["fields"]["trackingNumber"] == "6100999999"
    # no floor when a manual run names orders (created_after=None)
    assert plan_close({**SS, "createDate": "2026-05-26T10:00:00"}, FIN, SHIPPED, created_after=None)["action"] == "close"


class FakeSS:
    def __init__(self, fail=False): self.calls = []; self.fail = fail
    def mark_as_shipped(self, order_id, fields):
        if self.fail: raise RuntimeError("ShipStation 500")
        self.calls.append((order_id, fields)); return {"orderId": order_id}


class FakeFinale:
    def __init__(self, orders): self._orders = orders
    def get_order(self, po): return self._orders.get(po, {}).get("order")
    def order_shipments(self, o): return self._orders.get(o["orderId"], {}).get("ships", [])


def _run(ss_orders, fin, live, ss=None, existing=None, **kw):
    ss = ss or FakeSS()
    with patch("app.tracking.get_shipstation_marks", return_value=existing or {}), \
         patch("app.tracking.record_shipstation_mark") as rec:
        out = push_shipstation_close(ss_orders, live=live, client=ss, finale=fin, **kw)
    return out, ss, rec


def test_dry_reports_and_writes_nothing():
    fin = FakeFinale({"40842996": {"order": FIN, "ships": SHIPPED}})
    out, ss, rec = _run([SS], fin, False, created_after="2026-09-01")
    r = out["results"][0]
    assert r["status"] == "would_close" and r["fields"]["trackingNumber"] == "6100994307" and out["mode"] == "dry"
    assert ss.calls == [] and rec.assert_not_called() is None


def test_live_closes_with_pro_and_ship_date_and_receipts_once():
    fin = FakeFinale({"40842996": {"order": FIN, "ships": SHIPPED}})
    out, ss, rec = _run([SS], fin, True, created_after="2026-09-01")
    assert out["results"][0]["status"] == "close_done" and out["summary"]["close_done"] == 1
    assert ss.calls == [(323160921, {"carrierCode": "other", "trackingNumber": "6100994307", "shipDate": "2026-09-16"})]
    rec.assert_called_once_with("323160921", "40842996", "6100994307", "2026-09-16", "closed")
    # a receipt means done: nothing re-sent
    out2, ss2, rec2 = _run([SS], fin, True, existing={"323160921": {"status": "closed", "updated_at": "x"}})
    assert out2["results"][0]["status"] == "skipped_done" and ss2.calls == [] and rec2.assert_not_called() is None


def test_waiting_cancelled_and_prefloor_are_not_written_and_not_receipted():
    orders = [SS, {**SS, "orderId": 2, "orderNumber": "40865972"}, {**SS, "orderId": 3, "orderNumber": "40847423"},
              {**SS, "orderId": 4, "orderNumber": "40841465", "createDate": "2026-05-26T10:00:00"}]
    fin = FakeFinale({"40842996": {"order": FIN, "ships": SHIPPED},
                      "40865972": {"order": {**FIN, "orderId": "40865972"}, "ships": PACKED},
                      "40847423": {"order": {**FIN, "orderId": "40847423", "statusId": "ORDER_CANCELLED"}, "ships": []}})
    out, ss, rec = _run(orders, fin, True, created_after="2026-09-01")
    by = {r["po_number"]: r["status"] for r in out["results"]}
    assert by == {"40842996": "close_done", "40865972": "skipped_not_shipped", "40847423": "skipped_cancelled", "40841465": "skipped_floor"}
    assert [c[0] for c in ss.calls] == [323160921] and rec.call_count == 1


def test_cap_refuses_the_whole_run_and_failure_does_not_stop_the_batch():
    orders = [SS, {**SS, "orderId": 2, "orderNumber": "40864289"}]
    fin = FakeFinale({"40842996": {"order": FIN, "ships": SHIPPED}, "40864289": {"order": {**FIN, "orderId": "40864289"}, "ships": SHIPPED}})
    out, ss, _ = _run(orders, fin, True, created_after=None, max_per_run=1)
    assert out["blocked"].startswith("2 orders to close exceeds max_per_run 1") and ss.calls == []
    assert all(r["status"] == "would_close" for r in out["results"])
    out2, _, rec2 = _run(orders, fin, True, ss=FakeSS(fail=True), created_after=None)
    assert all(r["status"] == "failed" and "500" in r["error"] for r in out2["results"]); rec2.assert_not_called()
    # only= narrows to named order numbers; limit must be >= 1
    out3, ss3, _ = _run(orders, fin, True, only=["40864289"], created_after=None)
    assert [r["po_number"] for r in out3["results"]] == ["40864289"] and ss3.calls[0][0] == 2
    with pytest.raises(ValueError):
        push_shipstation_close(orders, live=False, limit=0)


def test_client_mark_as_shipped_payload_and_paging():
    with patch.dict("os.environ", {"SHIPSTATION_V1_KEY": "k", "SHIPSTATION_V1_SECRET": "s"}):
        assert ShipStationClient.configured()
        c = ShipStationClient()
    posts, gets = [], []
    def post(url, json=None, timeout=None):
        posts.append((url, json)); r = MagicMock(); r.status_code = 200; r.json.return_value = {"orderId": json["orderId"]}; return r
    def get(url, params=None, timeout=None):
        gets.append((url, dict(params))); r = MagicMock(); r.status_code = 200
        r.json.return_value = {"orders": [{"orderId": 1, "orderStatus": params["orderStatus"]}], "pages": 2 if params["page"] == 1 else 2}
        return r
    c.session.post, c.session.get = post, get
    with pytest.raises(RuntimeError):                                              # the in-test guard, always on under pytest
        c.mark_as_shipped(323160921, {"carrierCode": "other"})
    with patch.dict("os.environ", {"PYTEST_CURRENT_TEST": "", "PYTEST_RUNNING": ""}):  # lifted only to exercise the payload
        c.mark_as_shipped(323160921, {"carrierCode": "other", "trackingNumber": "6100994307", "shipDate": "2026-09-16"})
    assert posts == [("https://ssapi.shipstation.com/orders/markasshipped",
                      {"orderId": 323160921, "notifyCustomer": False, "notifySalesChannel": False,
                       "carrierCode": "other", "trackingNumber": "6100994307", "shipDate": "2026-09-16"})]
    orders = c.list_open_orders(2746880)
    assert len(orders) == 8 and all(g[1]["storeId"] == 2746880 for g in gets)          # 4 statuses x 2 pages
    assert {g[1]["orderStatus"] for g in gets} == {"awaiting_payment", "awaiting_shipment", "pending_fulfillment", "on_hold"}
    with patch.dict("os.environ", {"SHIPSTATION_V1_KEY": "", "SHIPSTATION_V1_SECRET": ""}):
        assert not ShipStationClient.configured()


def test_receipts_round_trip(tmp_path, monkeypatch):
    from app import tracking
    monkeypatch.setenv("TRACKING_DB", str(tmp_path / "t.db")); tracking.init_db()
    tracking.record_shipstation_mark("323160921", "40842996", "6100994307", "2026-09-16", "closed")
    got = tracking.get_shipstation_marks(["323160921", "x"])
    assert set(got) == {"323160921"} and got["323160921"]["order_number"] == "40842996" and got["323160921"]["status"] == "closed"
    assert tracking.get_shipstation_marks([]) == {}
