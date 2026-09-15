"""
Tests for the Finale invoice engine (app.finale_invoice). No network: config is
patched and Finale is a fake client. Locks down the EDI-driven build (850 product /
810 qty+price, exact CRSTL discount, our tax), the reconcile + qty gates that decide
posted-vs-draft, idempotency, the unresolved-id refusal, and tracking receipts.
"""
import pytest
from unittest.mock import patch

from app.finale_invoice import (build_finale_invoice, build_product_lines, money, push_finale_invoices,
                                qty_check, resolve_finale_refs)

CONFIG = {
    "dsd_stores": {"VAUGHAN": {"customer_id": "4115", "province": "ON", "tax_code": "CA-HST ONT", "tax_rate": 0.13}},
    "dropship_provinces": {
        "ON": {"customer_id": "4147", "tax_code": "CA-HST ONT", "tax_rate": 0.13},
        "QC": {"customer_id": "4146", "tax_code": "CA-S-QST", "tax_rate": 0.14975, "tax_components": [0.05, 0.09975]},
    },
    "dropship_blinds": {"ON": "4189"},
    "item": "Drapery Panels", "currency": "CAD",
    "channel_discounts": {"dsd": {"item": "d", "rate": 0.0618875, "note": ""},
                          "dropship": {"item": "d", "rate": 0.0518875, "note": ""}},
    "amount_guards": {"dsd": {"floor": 1, "ceiling": 100000}, "dropship": {"floor": 1, "ceiling": 100000}},
}
REFS = {"finale": {
    "promo_ids": {"dsd": "100037", "dropship": "100038"},
    "promo_desc": {"dsd": "DSD - 6.18875%", "dropship": "Dropship - 5.18875%"},
    "tax_rate_ids": {"ON": "100002", "QC": "100003"},
    "tax_desc": {"100002": "HST 13%", "100003": "GST + QST"},
}}
REFS_NO_TAX = {"finale": {**REFS["finale"], "tax_rate_ids": {}}}
ACCT = "hddecorating"

# A dropship ON blind: gross 20.00, CRSTL reduction 1.04 (5.19%), HST 13% on 18.96 = 2.46, total 21.42
INV = {"transaction_id": "T1", "source_document_id": "S1", "invoice_number": "INV1", "po_number": "PO1",
       "status": "Accepted", "product": "Blind", "store": None, "province": "ON",
       "invoice_date": "2026-09-15", "due_date": "", "subtotal": 20.00,
       "allowance_amount": 0.70, "discount_amount": 0.34, "total_amount": 21.42,
       "invoice_lines": [{"line_item_number": "10", "quantity": 1.0, "unit_price": 20.0, "upc": ""}]}
PO_MAP = {"PO1": {"province": "ON", "store": None, "vendor_items": ["138VB48D36WHTC"],
                  "lines": [{"line_item_number": "10", "sku": "1001987462", "upc": "069556590502",
                             "vendor_item": "138VB48D36WHTC", "quantity": 1.0}]}}
INDEX = {"1001987462": "/hddecorating/api/product/138VB5236WHTC",
         "069556590502": "/hddecorating/api/product/138VB5236WHTC"}


class FakeFinale:
    account_id = ACCT
    def __init__(self, order=True, invoices=None, shipped=None, fail_create=False):
        self.calls = []
        self._order = {"orderId": "PO1", "invoiceUrlList": [], "shipmentUrlList": []} if order else None
        self._invoices = invoices or []
        self._shipped = shipped
        self.fail_create = fail_create
    def product_index(self): return INDEX
    def get_order(self, oid): self.calls.append(("get_order", oid)); return self._order
    def order_invoices(self, order): return self._invoices
    def shipment_qty_for_order(self, order): return self._shipped
    def create_invoice(self, body):
        self.calls.append(("create", body))
        if self.fail_create: raise RuntimeError("boom")
        return {"invoiceId": "100407", "invoiceUrl": "/hddecorating/api/invoice/100407", "invoiceIdUser": "PO1-1"}
    def complete_invoice(self, url): self.calls.append(("complete", url)); return {"statusId": "INVOICE_APPROVED"}
    def complete_order(self, order):
        self.calls.append(("complete_order", order.get("orderId")))
        if getattr(self, "fail_complete_order", False): raise RuntimeError("order boom")
        return {"statusId": "ORDER_COMPLETED"}


@pytest.fixture(autouse=True)
def _cfg():
    with patch("app.netsuite._load_config", return_value=CONFIG):
        yield


def _live(client, refs=REFS, inv=INV, po_map=PO_MAP):
    with patch("app.tracking.get_finale_invoices", return_value={}) as _, \
         patch("app.tracking.record_finale_invoice") as rec_inv, \
         patch("app.tracking.record_events") as rec_ev:
        out = push_finale_invoices([inv], po_map, live=True, refs=refs, client=client)
    return out, rec_inv, rec_ev


def test_resolve_refs_dropship_and_dsd_tax_province():
    r = resolve_finale_refs(INV, REFS, CONFIG)
    assert r["channel"] == "dropship" and r["province"] == "ON"
    assert r["promo_id"] == "100038" and r["tax_id"] == "100002" and r["tax_rate"] == 0.13
    dsd = {**INV, "store": "VAUGHAN", "product": "Drape Panel"}
    r2 = resolve_finale_refs(dsd, REFS, CONFIG)
    assert r2["channel"] == "dsd" and r2["province"] == "ON" and r2["promo_id"] == "100037"  # tax follows the store's province


def test_money_books_exact_crstl_reduction_and_tax_on_net():
    m = money(INV, resolve_finale_refs(INV, REFS, CONFIG), CONFIG)
    assert m == {"gross": 20.0, "discount": -1.04, "net": 18.96, "tax": 2.46, "total": 21.42,
                 "hd_total": 21.42, "delta": 0.0}


def test_money_compound_province_rounds_each_component():
    qc = {**INV, "province": "QC", "product": "Drape Panel", "total_amount": 21.8}
    m = money(qc, resolve_finale_refs(qc, REFS, CONFIG), CONFIG)
    assert m["tax"] == round(round(18.96 * 0.05, 2) + round(18.96 * 0.09975, 2), 2) == 2.84


def test_product_lines_join_850_on_line_number_sku_then_upc():
    lines, missing = build_product_lines(INV, PO_MAP["PO1"]["lines"], INDEX)
    assert missing == [] and lines == [{"invoiceItemTypeId": "INV_PROD_ITEM",
                                        "productUrl": "/hddecorating/api/product/138VB5236WHTC",
                                        "quantity": 1.0, "unitPrice": 20.0, "itemDescription": "138VB48D36WHTC"}]
    # sku unknown -> falls back to the 850 UPC; neither -> reported, never guessed
    _, miss2 = build_product_lines(INV, PO_MAP["PO1"]["lines"], {"069556590502": "/p/x"})
    assert miss2 == []
    _, miss3 = build_product_lines(INV, PO_MAP["PO1"]["lines"], {})
    assert miss3 == ["10"]


def test_build_body_is_exact_create_shape():
    b = build_finale_invoice(INV, PO_MAP["PO1"], INDEX, REFS, ACCT, CONFIG)
    assert b["status"] == "built" and b["reconcile_flag"] is None and b["unresolved"] == []
    body = b["body"]
    assert body["invoiceUrl"] is None and body["invoiceTypeId"] == "SALES_INVOICE"
    assert body["primaryOrderUrl"] == "/hddecorating/api/order/PO1"
    assert body["invoiceDate"] == "2026-09-15T16:00:00.000Z"
    types = [i["invoiceItemTypeId"] for i in body["invoiceItemList"]]
    assert types == ["INV_PROD_ITEM", "INV_PROMOTION_ADJ", "INV_SALES_TAX"]
    promo, tax = body["invoiceItemList"][1], body["invoiceItemList"][2]
    assert promo["amount"] == -1.04 and promo["productPromoUrl"] == "/hddecorating/api/productpromo/100038"
    assert tax["amount"] == 2.46 and tax["taxAuthorityRateProductUrl"] == "/hddecorating/api/taxauthorityrateproduct/100002"


def test_build_flags_total_off_810_and_missing_po():
    off = build_finale_invoice({**INV, "total_amount": 22.00}, PO_MAP["PO1"], INDEX, REFS, ACCT, CONFIG)
    assert off["reconcile_flag"] == "total off CRSTL 810 by -0.58"
    assert build_finale_invoice(INV, {"lines": []}, INDEX, REFS, ACCT, CONFIG)["status"] == "skipped_no_po"
    assert build_finale_invoice({**INV, "province": "XX"}, PO_MAP["PO1"], INDEX, REFS, ACCT, CONFIG)["status"] == "skipped_no_map"


def test_qty_check_unverified_vs_mismatch_vs_ok():
    lines = [{"invoiceItemTypeId": "INV_PROD_ITEM", "productUrl": "/p/a", "quantity": 2}]
    assert qty_check(lines, None) == ([], False)
    assert qty_check(lines, {"/p/a": 2}) == ([], True)
    mism, ok = qty_check(lines, {"/p/a": 1})
    assert ok and mism == ["a: invoiced 2 vs shipped 1"]


def test_dry_run_builds_without_touching_finale():
    client = FakeFinale()
    with patch("app.tracking.record_events") as rec:
        out = push_finale_invoices([INV], PO_MAP, live=False, refs=REFS, client=client)
    assert out["mode"] == "dry" and out["summary"]["built"] == 1 and out["summary"]["posted"] == 0
    assert out["results"][0]["status"] == "built" and out["results"][0]["total"] == 21.42
    assert client.calls == [] and rec.assert_not_called() is None


def test_live_clean_invoice_is_created_and_posted_with_receipts():
    client = FakeFinale(shipped={"/hddecorating/api/product/138VB5236WHTC": 1.0})
    out, rec_inv, rec_ev = _live(client)
    r = out["results"][0]
    assert r["status"] == "posted" and r["invoice_id_user"] == "PO1-1" and r["qty_flag"] is None
    assert [c[0] for c in client.calls] == ["get_order", "create", "complete", "complete_order"]
    assert r["order_completed"] is True                                   # posted -> order completed
    assert client.calls[1][1]["invoiceItemList"][1]["amount"] == -1.04     # exact cents sent
    rec_inv.assert_called_once_with("T1", "PO1", "100407", "/hddecorating/api/invoice/100407", "PO1-1", "posted")
    rec_ev.assert_called_once_with(["T1"], "finale")
    assert out["summary"]["posted"] == 1 and out["summary"]["draft"] == 0


def test_live_total_off_810_stays_draft_never_completes():
    client = FakeFinale(shipped={"/hddecorating/api/product/138VB5236WHTC": 1.0})
    out, rec_inv, _ = _live(client, inv={**INV, "total_amount": 22.00})
    assert out["results"][0]["status"] == "draft"
    assert "complete" not in [c[0] for c in client.calls] and "complete_order" not in [c[0] for c in client.calls]
    assert rec_inv.call_args.args[-1] == "draft"


def test_live_qty_mismatch_stays_draft_but_unshipped_is_skipped_for_retry():
    mism = FakeFinale(shipped={"/hddecorating/api/product/138VB5236WHTC": 3.0})
    out, _, _ = _live(mism)
    assert out["results"][0]["status"] == "draft"
    assert out["results"][0]["qty_flag"].startswith("shipped qty != 810")
    # not shipped in Finale yet: NO invoice, NO receipt -> the next poll retries it
    unshipped = FakeFinale(shipped=None)
    out2, rec_inv, rec_ev = _live(unshipped)
    assert out2["results"][0]["status"] == "skipped_not_shipped"
    assert "create" not in [c[0] for c in unshipped.calls]
    rec_inv.assert_not_called(); rec_ev.assert_not_called()
    assert out2["summary"]["skipped_not_shipped"] == 1


def test_order_complete_failure_never_unposts_the_invoice():
    client = FakeFinale(shipped={"/hddecorating/api/product/138VB5236WHTC": 1.0})
    client.fail_complete_order = True
    out, rec_inv, _ = _live(client)
    r = out["results"][0]
    assert r["status"] == "posted" and r["order_completed"] is False and "order boom" in r["order_complete_error"]
    assert rec_inv.call_args.args[-1] == "posted"


def test_live_is_idempotent_on_receipt_and_on_existing_order_invoice():
    client = FakeFinale(shipped={"/hddecorating/api/product/138VB5236WHTC": 1.0})
    with patch("app.tracking.get_finale_invoices",
               return_value={"T1": {"invoice_id": "1", "invoice_url": "/i/1", "invoice_id_user": "PO1-1", "status": "posted"}}), \
         patch("app.tracking.record_events") as rec:
        out = push_finale_invoices([INV], PO_MAP, live=True, refs=REFS, client=client)
    assert out["results"][0]["status"] == "skipped_exists" and client.calls == []
    rec.assert_not_called()
    # a live (non-cancelled) invoice already on the order -> skip; a cancelled one doesn't count
    client2 = FakeFinale(invoices=[{"invoiceId": "9", "invoiceIdUser": "PO1-1", "statusId": "INVOICE_APPROVED"}],
                         shipped={"/hddecorating/api/product/138VB5236WHTC": 1.0})
    out2, _, _ = _live(client2)
    assert out2["results"][0]["status"] == "skipped_exists" and "create" not in [c[0] for c in client2.calls]
    client3 = FakeFinale(invoices=[{"invoiceId": "9", "statusId": "INVOICE_CANCELLED"}],
                         shipped={"/hddecorating/api/product/138VB5236WHTC": 1.0})
    out3, _, _ = _live(client3)
    assert out3["results"][0]["status"] == "posted"


def test_live_missing_order_and_missing_product_are_skipped():
    out, _, _ = _live(FakeFinale(order=False))
    assert out["results"][0]["status"] == "skipped_no_order"
    client = FakeFinale()
    with patch("app.tracking.get_finale_invoices", return_value={}), patch("app.tracking.record_events"):
        out2 = push_finale_invoices([INV], PO_MAP, live=True, refs=REFS, client=client, product_index={})
    assert out2["results"][0]["status"] == "skipped_no_product" and client.calls == []


def test_live_refused_while_ids_unresolved():
    client = FakeFinale()
    with patch("app.tracking.record_events") as rec:
        out = push_finale_invoices([INV], PO_MAP, live=True, refs=REFS_NO_TAX, client=client)
    assert out.get("blocked") == "unresolved ids" and out["unresolved"] == ["taxrate:ON"]
    assert client.calls == [] and rec.assert_not_called() is None


def test_live_one_failure_does_not_stop_the_batch():
    inv2 = {**INV, "transaction_id": "T2", "source_document_id": "S2", "invoice_number": "INV2", "po_number": "PO2"}
    po_map = {**PO_MAP, "PO2": PO_MAP["PO1"]}
    client = FakeFinale(shipped={"/hddecorating/api/product/138VB5236WHTC": 1.0})
    orig = client.create_invoice
    def flaky(body):
        if body["primaryOrderUrl"].endswith("PO1"): raise RuntimeError("boom")
        return orig(body)
    client.create_invoice = flaky
    with patch("app.tracking.get_finale_invoices", return_value={}), \
         patch("app.tracking.record_finale_invoice"), patch("app.tracking.record_events") as rec:
        out = push_finale_invoices([INV, inv2], po_map, live=True, refs=REFS, client=client)
    by = {r["transaction_id"]: r["status"] for r in out["results"]}
    assert by == {"T1": "failed", "T2": "posted"}
    rec.assert_called_once_with(["T2"], "finale")


def test_tracking_finale_receipts_roundtrip(tmp_path, monkeypatch):
    from app import tracking
    monkeypatch.setenv("TRACKING_DB", str(tmp_path / "t.db")); tracking.init_db()
    tracking.record_finale_invoice("T1", "PO1", "100407", "/i/100407", "PO1-1", "draft")
    tracking.record_finale_invoice("T1", "PO1", "100407", "/i/100407", "PO1-1", "posted")   # upsert
    got = tracking.get_finale_invoices(["T1", "T9"])
    assert set(got) == {"T1"} and got["T1"]["status"] == "posted" and got["T1"]["invoice_id_user"] == "PO1-1"
    tracking.record_events(["T1"], "finale")
    assert tracking.get_unfinaled_ids(["T1", "T2"]) == ["T2"]
    assert tracking.get_latest_events(["T1"])["T1"]["finale_at"] is not None


def test_dry_run_without_client_resolves_products_read_only():
    """A dry run with no client fetches the product catalogue (GET only) so the
    preview shows real product resolution instead of 'missing' everywhere."""
    with patch("app.finale.FinaleClient") as FC:
        FC.configured.return_value = True
        FC.return_value.product_index.return_value = INDEX
        out = push_finale_invoices([INV], PO_MAP, live=False, refs=REFS)
    FC.return_value.product_index.assert_called_once()
    assert out["results"][0]["status"] == "built" and out["results"][0]["missing_products"] == []
