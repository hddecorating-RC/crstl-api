"""Actual ship dates from Finale, for the DSD rows the 856 cannot fill.

The shapes asserted here are real: Finale returns lists column-major, its
shipDate carries a fixed clock time rather than a real one, and the date is on
the shipment rather than the order. A test built on invented JSON would pass
against none of that.
"""
import pytest

from app.finale import (FinaleClient, FinaleUnavailable, ship_date_index,
                        shipped_date_of, to_rows)


def test_lists_come_back_column_major():
    """Finale returns {field: [v1, v2]}, not [{field: v1}, {field: v2}]."""
    assert to_rows({"a": [1, 2], "b": ["x", "y"]}) == [{"a": 1, "b": "x"},
                                                       {"a": 2, "b": "y"}]


def test_a_single_object_is_one_row():
    assert to_rows({"a": 1, "b": "x"}) == [{"a": 1, "b": "x"}]


def test_an_already_row_major_list_passes_through():
    assert to_rows([{"a": 1}]) == [{"a": 1}]


def test_empty_response():
    assert to_rows({}) == []


def test_the_clock_time_on_ship_date_is_discarded():
    """shipDate reads 16:00:00 or 19:00:00 and never anything else -- it is a
    date at a fixed offset, not a moment. Calibrated against 56 Dropship ASNs,
    whose ASN date is the known-real ship date, the date part matched exactly
    on 49 and was one day later on 7. Shifting it a day would move the bulk
    the wrong way."""
    assert shipped_date_of({"shipDate": "2026-06-22T19:00:00"}) == "2026-06-22"
    assert shipped_date_of({"shipDate": "2026-08-18T16:00:00"}) == "2026-08-18"
    assert shipped_date_of({"shipDate": None}) == ""
    assert shipped_date_of({}) == ""


def _ship(po, date, status="SHIPMENT_SHIPPED"):
    return {"primaryOrderUrl": f"/hddecorating/api/order/{po}",
            "shipDate": f"{date}T16:00:00" if date else date, "statusId": status}


def test_delivered_shipments_have_shipped_too():
    """Filtering to SHIPMENT_SHIPPED alone drops 122 of 326 moved records."""
    index = ship_date_index([_ship("40858045", "2026-08-18", "SHIPMENT_DELIVERED")])
    assert index == {"40858045": "2026-08-18"}


def test_unshipped_and_cancelled_records_are_ignored():
    for status in ("SHIPMENT_INPUT", "SHIPMENT_PACKED", "SHIPMENT_CANCELLED"):
        assert ship_date_index([_ship("P1", "2026-08-18", status)]) == {}


def test_the_order_id_in_the_url_is_the_hd_po_number():
    index = ship_date_index([_ship("538637328", "2026-09-03")])
    assert index == {"538637328": "2026-09-03"}


def test_a_split_shipment_reports_the_last_movement():
    """7 POs carry more than one shipment. The order is not fully shipped until
    the last one leaves, so an earlier date would overstate how long ago it
    went."""
    index = ship_date_index([_ship("P1", "2026-03-24"), _ship("P1", "2026-03-26")])
    assert index == {"P1": "2026-03-26"}


def test_a_shipment_with_no_order_or_no_date_is_skipped():
    assert ship_date_index([{"primaryOrderUrl": "", "shipDate": "2026-01-01T16:00:00",
                             "statusId": "SHIPMENT_SHIPPED"}]) == {}
    assert ship_date_index([_ship("P1", None)]) == {}


def test_a_client_without_credentials_refuses_rather_than_guessing():
    with pytest.raises(FinaleUnavailable):
        FinaleClient(account_id="", api_key="", api_secret="")


def test_configured_reports_whether_the_report_can_ask_finale(monkeypatch):
    for k in ("FINALE_ACCOUNT_ID", "FINALE_API_KEY", "FINALE_API_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert FinaleClient.configured() is False
    for k in ("FINALE_ACCOUNT_ID", "FINALE_API_KEY", "FINALE_API_SECRET"):
        monkeypatch.setenv(k, "x")
    assert FinaleClient.configured() is True


def test_invoice_total_and_approved_by_read_a_finale_invoice():
    """The money on an existing Finale invoice (any creator): product lines are
    unitPrice x quantity, tax/promo lines carry an amount. approved_by is the login
    on the INVOICE_APPROVED history entry; created_by the first entry."""
    from app.finale import approved_by, created_by, invoice_total
    inv = {"statusId": "INVOICE_APPROVED",
           "invoiceItemList": [
               {"invoiceItemTypeId": "INV_PROD_ITEM", "unitPrice": 38.5, "quantity": 2},
               {"invoiceItemTypeId": "INV_SALES_TAX", "amount": 9.49},
               {"invoiceItemTypeId": "INV_PROMOTION_ADJ", "amount": -4}],
           "statusIdHistoryList": [
               {"statusId": None, "txStamp": 1, "userLoginUrl": "/hddecorating/api/userlogin/edward.schiavon"},
               {"statusId": "INVOICE_APPROVED", "txStamp": 2, "userLoginUrl": "/hddecorating/api/userlogin/api_key_u_blinds"}]}
    assert invoice_total(inv) == 82.49
    assert created_by(inv) == "edward.schiavon" and approved_by(inv) == "api_key_u_blinds"
    assert invoice_total({"invoiceItemList": [{"invoiceItemTypeId": "INV_PROD_ITEM", "unitPrice": "x"}]}) == 0.0
    assert approved_by({"statusId": "INVOICE_IN_PROCESS", "statusIdHistoryList": [{"statusId": None}]}) == ""


def test_wanted_carrier_and_carrier_fix():
    """Carrier defaults resolve by exact Finale name, only for the two EDI channels;
    an unknown name is reported, never guessed; carrier_fix lists the shipments a
    write would touch."""
    from app.finale import carrier_fix, wanted_carrier
    cfg = {"carriers": {"enabled": True, "dsd": "HDOC", "dropship": "Purolator Canada"}}
    idx = {"HDOC": "/h/api/partygroup/100021", "Purolator Canada": "/h/api/partygroup/100029"}
    assert wanted_carrier(cfg, "dropship", idx) == {"name": "Purolator Canada", "url": "/h/api/partygroup/100029", "enabled": True, "reason": ""}
    assert wanted_carrier(cfg, "dsd", idx)["url"] == "/h/api/partygroup/100021"
    assert wanted_carrier(cfg, "nonedi", idx)["name"] is None                 # never for a non-EDI channel
    miss = wanted_carrier(cfg, "dropship", {"HDOC": "/x"})
    assert miss["url"] is None and "not on Finale's Carriers list" in miss["reason"]
    assert wanted_carrier({"carriers": {"dsd": "HDOC"}}, "dsd", idx)["enabled"] is False
    ships = [{"shipmentUrl": "/s/1", "statusId": "SHIPMENT_SHIPPED", "carrierPartyUrl": None},
             {"shipmentUrl": "/s/2", "statusId": "SHIPMENT_SHIPPED", "carrierPartyUrl": "/h/api/partygroup/100029"},
             {"shipmentUrl": "/s/3", "statusId": "SHIPMENT_PACKED", "carrierPartyUrl": None},
             {"shipmentUrl": "/s/4", "statusId": "SHIPMENT_CANCELLED", "carrierPartyUrl": None}]
    assert [x["shipmentUrl"] for x in carrier_fix(ships, "/h/api/partygroup/100029")] == ["/s/1", "/s/3"]
    assert [x["shipmentUrl"] for x in carrier_fix(ships, "/h/api/partygroup/100029", ("SHIPMENT_SHIPPED",))] == ["/s/1"]
    assert carrier_fix(ships, None) == []


def test_carrier_index_reads_the_column_major_party_listing():
    from unittest.mock import patch
    from app.finale import FinaleClient
    listing = {"partyId": ["100021", "100029", "100043"], "partyUrl": ["/h/api/partygroup/100021", "/h/api/partygroup/100029", "/h/api/partygroup/100043"],
               "groupName": ["HDOC", "Purolator Canada", ""]}
    with patch.dict("os.environ", {"FINALE_ACCOUNT_ID": "h", "FINALE_API_KEY": "k", "FINALE_API_SECRET": "s"}):
        c = FinaleClient()
    with patch.object(c, "_get", return_value=listing):
        assert c.carrier_index() == {"HDOC": "/h/api/partygroup/100021", "Purolator Canada": "/h/api/partygroup/100029"}


def test_set_order_user_fields_merges_and_edits_only_when_locked():
    """Finale REPLACES userFieldDataList: the write resends every existing entry with
    ours merged in. LOCKED -> edit, write, lock; CREATED -> write; COMPLETED -> refused."""
    from unittest.mock import MagicMock, patch
    from app.finale import FinaleClient
    with patch.dict("os.environ", {"FINALE_ACCOUNT_ID": "h", "FINALE_API_KEY": "k", "FINALE_API_SECRET": "s"}):
        c = FinaleClient()
    calls = []
    state = {"after_write": {"orderUrl": "/h/api/order/1", "statusId": "ORDER_CREATED", "actionUrlLock": "/h/api/order/1/lock"},
             "after_lock": {"orderUrl": "/h/api/order/1", "statusId": "ORDER_LOCKED"}, "write_fails": False}
    def post(url, json=None, timeout=None):
        calls.append((url.replace(c.HOST, ""), json))
        r = MagicMock(); r.status_code = 200
        if url.endswith("/edit"): r.json.return_value = {"orderUrl": "/h/api/order/1", "statusId": "ORDER_CREATED", "actionUrlLock": "/h/api/order/1/lock"}
        elif url.endswith("/lock"): r.json.return_value = state["after_lock"]
        else:
            if state["write_fails"]: raise RuntimeError("write 400")
            r.json.return_value = {"orderUrl": "/h/api/order/1", "userFieldDataList": json["userFieldDataList"]}   # no actionUrlLock here
        return r
    def get(url, params=None, timeout=None):
        calls.append((url.replace(c.HOST, "") + " GET", None)); r = MagicMock(); r.status_code = 200; r.json.return_value = state["after_write"]; return r
    c.session.post, c.session.get = post, get
    locked = {"orderId": "1", "orderUrl": "/h/api/order/1", "statusId": "ORDER_LOCKED", "actionUrlEdit": "/h/api/order/1/edit",
              "userFieldDataList": [{"attrName": "integration_ssconnection_100000", "attrValue": "HASH"}, {"attrName": "user_10000", "attrValue": "old"}]}
    out = c.set_order_user_fields(locked, {"user_10000": "6100994307", "user_10001": "S1"})
    assert [u for u, _ in calls] == ["/h/api/order/1/edit", "/h/api/order/1", "/h/api/order/1 GET", "/h/api/order/1/lock"]   # re-read, then lock
    assert calls[1][1] == {"orderUrl": "/h/api/order/1", "userFieldDataList": [
        {"attrName": "integration_ssconnection_100000", "attrValue": "HASH"},        # kept
        {"attrName": "user_10000", "attrValue": "6100994307"},                        # updated in place
        {"attrName": "user_10001", "attrValue": "S1"}]}                               # added
    assert out["statusId"] == "ORDER_LOCKED"
    calls.clear()
    created = {**locked, "statusId": "ORDER_CREATED"}
    c.set_order_user_fields(created, {"user_10000": "x"})
    assert [u for u, _ in calls] == ["/h/api/order/1"]                                # no edit/lock cycle
    import pytest
    with pytest.raises(ValueError):
        c.set_order_user_fields({**locked, "statusId": "ORDER_COMPLETED"}, {"user_10000": "x"})
    assert len(calls) == 1                                                            # nothing sent
    # the order is still CREATED after the lock: refused loudly, never receipted as done
    calls.clear(); state["after_lock"] = {"orderUrl": "/h/api/order/1", "statusId": "ORDER_CREATED"}
    with pytest.raises(RuntimeError, match="not ORDER_LOCKED"):
        c.set_order_user_fields(locked, {"user_10000": "x"})
    # the write fails: the order is still re-locked, and the WRITE error is what surfaces
    calls.clear(); state["after_lock"] = {"orderUrl": "/h/api/order/1", "statusId": "ORDER_LOCKED"}; state["write_fails"] = True
    with pytest.raises(RuntimeError, match="write 400"):
        c.set_order_user_fields(locked, {"user_10000": "x"})
    assert "/h/api/order/1/lock" in [u for u, _ in calls]


# ── rate limit (2026-09-21) ────────────────────────────────────────────────

def _resp(status, reset_ms=None):
    import requests
    r = requests.Response()
    r.status_code = status
    if reset_ms is not None:
        r.headers["X-RateLimit-Reset"] = str(reset_ms)
    r._content = b"{}"
    import io
    r.raw = io.BytesIO(b"{}")          # a real response has a stream the retry closes
    return r


def test_rate_limit_wait_reads_the_reset_header_and_is_bounded():
    from app.finale import rate_limit_wait
    assert rate_limit_wait({"X-RateLimit-Reset": "1000030000"}, now=1000000.0) == pytest.approx(30.5)
    assert rate_limit_wait({"X-RateLimit-Reset": "999000000"}, now=1000000.0) == 1.0     # already past
    assert rate_limit_wait({"X-RateLimit-Reset": "9999999999999"}, now=1000000.0) == 65.0
    assert rate_limit_wait({}, now=1000000.0) == 20.0


def test_a_rate_limited_get_waits_and_retries_but_a_post_never_does():
    import requests
    from unittest.mock import patch as _patch
    from app.finale import RateLimitRetry
    adapter = RateLimitRetry()
    get = requests.Request("GET", "https://app.finaleinventory.com/x/api/shipment/1").prepare()
    post = requests.Request("POST", "https://app.finaleinventory.com/x/api/shipment/1", json={}).prepare()
    with _patch("requests.adapters.HTTPAdapter.send", side_effect=[_resp(429), _resp(200)]) as send, \
         _patch("app.finale.time.sleep") as sleep:
        assert adapter.send(get).status_code == 200
    assert send.call_count == 2 and sleep.call_count == 1
    with _patch("requests.adapters.HTTPAdapter.send", side_effect=[_resp(429)]) as send, \
         _patch("app.finale.time.sleep") as sleep:
        assert adapter.send(post).status_code == 429
    assert send.call_count == 1 and sleep.call_count == 0
    with _patch("requests.adapters.HTTPAdapter.send", side_effect=[_resp(429)] * 4) as send, \
         _patch("app.finale.time.sleep"):
        assert adapter.send(get).status_code == 429                          # gives up: raises as before
    assert send.call_count == 1 + RateLimitRetry.MAX_RETRIES


def test_every_finale_request_goes_through_the_rate_limit_adapter():
    from app.finale import RateLimitRetry
    c = FinaleClient(account_id="a", api_key="k", api_secret="s")
    assert isinstance(c.session.get_adapter("https://app.finaleinventory.com/a/api/order/1"), RateLimitRetry)


def test_a_full_list_stops_instead_of_acting_on_part_of_it():
    """Finale's collection endpoints do not page; an answer with as many rows as we
    asked for is probably cut off, and acting on it would skip orders silently."""
    from unittest.mock import patch as _patch
    from app.finale import FinaleListFull
    c = FinaleClient(account_id="a", api_key="k", api_secret="s")
    full = {"shipmentUrl": ["/s/%d" % i for i in range(3)], "statusId": ["SHIPMENT_PACKED"] * 3}
    with _patch.object(FinaleClient, "PAGE_LIMIT", 3), _patch.object(c, "_get", return_value=full):
        for call in (c.list_shipments, c.list_sale_orders, c.product_index, c.party_province_index):
            with pytest.raises(FinaleListFull, match="probably incomplete"):
                call()
    with _patch.object(FinaleClient, "PAGE_LIMIT", 4), _patch.object(c, "_get", return_value=full):
        assert len(c.list_shipments()) == 3
