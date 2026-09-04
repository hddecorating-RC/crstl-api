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
