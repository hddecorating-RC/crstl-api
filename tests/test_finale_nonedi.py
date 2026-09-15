"""
Tests for the non-EDI Finale invoicer (app.finale_nonedi): classification by exclusion,
the order-line + shipped-qty build with party-province tax, the post/draft/skip gates,
idempotency, the cap, and receipts. No network: config patched, Finale is a fake.
"""
import pytest
from unittest.mock import patch

from app.finale_nonedi import (build_nonedi_invoice, is_non_edi, push_nonedi_invoices, receipt_key,
                               select_candidates)

CONFIG = {"dropship_provinces": {"ON": {"tax_rate": 0.13}, "QC": {"tax_rate": 0.14975, "tax_components": [0.05, 0.09975]}},
          "dsd_stores": {}, "channel_discounts": {}, "item": "x", "currency": "CAD"}
REFS = {"finale": {"tax_rate_ids": {"ON": "100002", "QC": "100003"}, "tax_desc": {"100002": "HST 13%", "100003": "GST + QST"}}}
ACCT = "hddecorating"
P_A, P_B = "/hddecorating/api/product/138VB4848WHTC", "/hddecorating/api/product/200029"
ORDER = {"orderId": "507872-00", "orderUrl": "/hddecorating/api/order/507872-00", "orderTypeId": "SALES_ORDER",
         "statusId": "ORDER_LOCKED", "saleSourceId": None, "orderDate": "2026-09-16",
         "orderRoleList": [{"roleTypeId": "CUSTOMER", "partyId": "100022"}],
         "orderItemList": [{"productUrl": P_A, "unitPrice": 23.35, "quantity": 24},
                           {"productUrl": P_B, "unitPrice": 10.00, "quantity": 2}],
         "invoiceUrlList": [], "shipmentUrlList": []}
CRSTL_POS = {"538831979", "40864264"}
PROV = {"100022": "ON"}


class FakeFinale:
    account_id = ACCT
    def __init__(self, order=ORDER, invoices=None, shipped=None):
        self.calls = []; self._order = order; self._invoices = invoices or []; self._shipped = shipped
    def party_province_index(self): return PROV
    def get_order(self, oid): self.calls.append(("get_order", oid)); return self._order
    def order_invoices(self, order): return self._invoices
    def shipment_qty_for_order(self, order): return self._shipped
    def create_invoice(self, body):
        self.calls.append(("create", body)); return {"invoiceId": "100420", "invoiceUrl": "/hddecorating/api/invoice/100420", "invoiceIdUser": "507872-00-1"}
    def complete_invoice(self, url): self.calls.append(("complete", url)); return {"statusId": "INVOICE_APPROVED"}
    def complete_order(self, order): self.calls.append(("complete_order", order.get("orderId"))); return {"statusId": "ORDER_COMPLETED"}
    def reopen_order(self, order):
        self.calls.append(("reopen", order.get("orderId"))); self._order = {**order, "statusId": "ORDER_LOCKED"}; return self._order


@pytest.fixture(autouse=True)
def _cfg():
    with patch("app.netsuite._load_config", return_value=CONFIG):
        yield


def _run(client, live, orders=(ORDER,), **kw):
    with patch("app.tracking.get_finale_invoices", return_value=kw.pop("existing", {})), \
         patch("app.tracking.record_finale_invoice") as rec_inv, patch("app.tracking.record_events") as rec_ev:
        out = push_nonedi_invoices(list(orders), CRSTL_POS, live=live, refs=REFS, client=client,
                                   floor="2026-09-15", today="2026-09-16", **kw)
    return out, rec_inv, rec_ev


def test_classification_is_by_exclusion_not_customer():
    assert is_non_edi(ORDER, CRSTL_POS)
    assert not is_non_edi({**ORDER, "orderId": "538831979"}, CRSTL_POS)            # a Crstl PO -> EDI
    assert not is_non_edi({**ORDER, "saleSourceId": "HD Dropship"}, CRSTL_POS)     # EDI source tag -> EDI
    assert is_non_edi({**ORDER, "saleSourceId": "HD Supply"}, CRSTL_POS)
    assert not is_non_edi({**ORDER, "orderId": "TEST_0005"}, CRSTL_POS)          # fixtures are never invoiced
    # completed / cancelled / pre-floor orders are never candidates
    orders = [ORDER, {**ORDER, "orderId": "x", "statusId": "ORDER_COMPLETED"},
              {**ORDER, "orderId": "y", "statusId": "ORDER_CANCELLED"}, {**ORDER, "orderId": "z", "orderDate": "2026-09-01"}]
    assert [o["orderId"] for o in select_candidates(orders, CRSTL_POS, floor="2026-09-15")] == ["507872-00"]
    assert select_candidates(orders, CRSTL_POS, floor="2026-09-15", only=["z", "507872-00"])[0]["orderId"] == "507872-00"


def test_build_uses_shipped_qty_order_price_and_party_province_tax():
    b = build_nonedi_invoice(ORDER, {P_A: 24.0, P_B: 2.0}, "ON", REFS, ACCT, CONFIG, today="2026-09-16")
    assert b["status"] == "built" and b["flag"] is None
    assert b["gross"] == 580.4 and b["discount"] == 0.0 and b["tax"] == 75.45 and b["total"] == 655.85
    items = b["body"]["invoiceItemList"]
    assert [i["invoiceItemTypeId"] for i in items] == ["INV_PROD_ITEM", "INV_PROD_ITEM", "INV_SALES_TAX"]
    assert items[0] == {"invoiceItemTypeId": "INV_PROD_ITEM", "productUrl": P_A, "quantity": 24.0, "unitPrice": 23.35}
    assert items[2]["taxAuthorityRateProductUrl"].endswith("/taxauthorityrateproduct/100002") and items[2]["amount"] == 75.45
    assert b["body"]["primaryOrderUrl"] == ORDER["orderUrl"] and b["body"]["invoiceDate"] == "2026-09-16T16:00:00.000Z"
    # partial ship: qty follows the shipment, not the order
    p = build_nonedi_invoice(ORDER, {P_A: 10.0}, "ON", REFS, ACCT, CONFIG, today="2026-09-16")
    assert p["gross"] == 233.5 and len(p["body"]["invoiceItemList"]) == 2
    # compound province rounds each component
    q = build_nonedi_invoice(ORDER, {P_B: 2.0}, "QC", REFS, ACCT, CONFIG, today="2026-09-16")
    assert q["tax"] == round(round(20 * 0.05, 2) + round(20 * 0.09975, 2), 2)


def test_build_skips_and_flags():
    assert build_nonedi_invoice(ORDER, {P_A: 1}, None, REFS, ACCT, CONFIG)["status"] == "skipped_no_province"
    assert build_nonedi_invoice(ORDER, {P_A: 1}, "XX", REFS, ACCT, CONFIG)["status"] == "skipped_no_map"
    assert build_nonedi_invoice(ORDER, None, "ON", REFS, ACCT, CONFIG)["status"] == "skipped_not_shipped"
    assert build_nonedi_invoice(ORDER, {P_A: 0}, "ON", REFS, ACCT, CONFIG)["status"] == "skipped_not_shipped"
    f = build_nonedi_invoice(ORDER, {P_A: 1, "/p/unknown": 3}, "ON", REFS, ACCT, CONFIG)
    assert f["status"] == "built" and "unknown" in f["flag"]        # shipped product the order doesn't price -> flagged


def test_dry_run_previews_without_writing():
    c = FakeFinale(shipped={P_A: 24.0, P_B: 2.0})
    out, rec_inv, rec_ev = _run(c, live=False)
    r = out["results"][0]
    assert out["mode"] == "dry" and r["status"] == "built" and r["would"] == "posted" and r["total"] == 655.85
    assert r["customer"] == "100022" and r["province"] == "ON"
    assert not [x for x in c.calls if x[0] in ("create", "complete", "complete_order")]
    rec_inv.assert_not_called(); rec_ev.assert_not_called()


def test_live_posts_completes_order_and_records_receipt():
    c = FakeFinale(shipped={P_A: 24.0, P_B: 2.0})
    out, rec_inv, rec_ev = _run(c, live=True)
    r = out["results"][0]
    assert r["status"] == "posted" and r["order_completed"] is True and r["invoice_id_user"] == "507872-00-1"
    assert [x[0] for x in c.calls] == ["get_order", "create", "complete", "complete_order"]
    assert c.calls[1][1]["invoiceItemList"][-1]["amount"] == 75.45
    rec_inv.assert_called_once_with(receipt_key("507872-00"), "507872-00", "100420", "/hddecorating/api/invoice/100420", "507872-00-1", "posted")
    rec_ev.assert_called_once_with([receipt_key("507872-00")], "finale")
    assert out["summary"]["posted"] == 1


def test_live_unpriced_product_is_draft_not_posted():
    c = FakeFinale(shipped={P_A: 24.0, "/p/unknown": 1.0})
    out, rec_inv, _ = _run(c, live=True)
    assert out["results"][0]["status"] == "draft" and "complete" not in [x[0] for x in c.calls]
    assert rec_inv.call_args.args[-1] == "draft"


def test_live_skips_not_shipped_no_province_existing_invoice_and_receipt():
    out, _, ev = _run(FakeFinale(shipped=None), live=True)
    assert out["results"][0]["status"] == "skipped_not_shipped"; ev.assert_not_called()
    no_prov = FakeFinale(order={**ORDER, "orderRoleList": [{"roleTypeId": "CUSTOMER", "partyId": "100026"}]}, shipped={P_A: 1.0})
    out2, _, _ = _run(no_prov, live=True)
    assert out2["results"][0]["status"] == "skipped_no_province" and "create" not in [x[0] for x in no_prov.calls]
    has_inv = FakeFinale(invoices=[{"invoiceId": "1", "invoiceIdUser": "507872-00-1", "statusId": "INVOICE_APPROVED"}], shipped={P_A: 1.0})
    out3, _, _ = _run(has_inv, live=True)
    assert out3["results"][0]["status"] == "skipped_exists"
    c4 = FakeFinale(shipped={P_A: 1.0})
    out4, _, _ = _run(c4, live=True, existing={receipt_key("507872-00"): {"status": "posted", "invoice_id": "9", "invoice_id_user": "507872-00-1"}})
    assert out4["results"][0]["status"] == "skipped_exists" and c4.calls == []


def test_cap_counts_invoices_about_to_be_created_and_missing_order_is_skipped():
    """Two shipped orders over a cap of 1: refused, nothing written, preview kept.
    One shipped + one waiting: the waiting one does not count, the shipped one posts."""
    two = [ORDER, {**ORDER, "orderId": "507873-00"}]
    c = FakeFinale(shipped={P_A: 1.0})
    out, rec, _ = _run(c, live=True, orders=two, max_per_run=1)
    assert out["blocked"].startswith("2 invoices to create exceeds max_per_run 1")
    assert "create" not in [x[0] for x in c.calls] and rec.assert_not_called() is None
    assert [r["would"] for r in out["results"]] == ["posted", "posted"]
    class ByOrder(FakeFinale):
        def get_order(self, oid):
            self._shipped = {P_A: 1.0} if oid == "507872-00" else None
            return {**self._order, "orderId": oid}
    out2, _, _ = _run(ByOrder(), live=True, orders=two, max_per_run=1)
    assert "blocked" not in out2 and [r["status"] for r in out2["results"]] == ["posted", "skipped_not_shipped"]
    out3, _, _ = _run(FakeFinale(order=None), live=True)
    assert out3["results"][0]["status"] == "skipped_no_order"


def test_hollow_completed_nonedi_order_is_a_candidate_and_reopens_when_gate_on():
    hollow = {**ORDER, "statusId": "ORDER_COMPLETED", "invoiceUrlList": []}
    assert select_candidates([hollow], CRSTL_POS, floor="2026-09-15") == []
    assert select_candidates([hollow], CRSTL_POS, floor="2026-09-15", include_hollow_completed=True) == [hollow]
    invoiced = {**hollow, "invoiceUrlList": ["/i/1"]}
    assert select_candidates([invoiced], CRSTL_POS, floor="2026-09-15", include_hollow_completed=True) == []
    c = FakeFinale(order=hollow, shipped={P_A: 24.0, P_B: 2.0})
    out, _, _ = _run(c, live=True, orders=[hollow])
    assert out["results"][0]["status"] == "skipped_completed" and out["results"][0]["would_reopen"] is True   # gate off by default
    dry, _, _ = _run(FakeFinale(order=hollow, shipped={P_A: 24.0, P_B: 2.0}), live=False, orders=[hollow], auto_reopen=True)
    assert dry["results"][0]["would"] == "posted" and dry["results"][0]["would_reopen"] is True              # gate on: preview, no reopen
    c2 = FakeFinale(order=hollow, shipped={P_A: 24.0, P_B: 2.0})
    out2, _, _ = _run(c2, live=True, orders=[hollow], auto_reopen=True)
    assert out2["results"][0]["status"] == "posted" and out2["results"][0]["reopened"] is True
    assert [x[0] for x in c2.calls] == ["get_order", "reopen", "create", "complete", "complete_order"]
