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
    def reopen_order(self, order):
        self.calls.append(("reopen", order.get("orderId")))
        self._order = {**order, "statusId": "ORDER_LOCKED"}; return self._order


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


def test_body_carries_the_810_invoice_number_as_reference():
    """Parity with the warehouse's manual DSD invoices (40864264-1: referenceNumber INV40864264)."""
    b = build_finale_invoice(INV, PO_MAP["PO1"], INDEX, REFS, ACCT, CONFIG)
    assert b["body"]["referenceNumber"] == "INV1"
    b2 = build_finale_invoice({**INV, "invoice_number": None}, PO_MAP["PO1"], INDEX, REFS, ACCT, CONFIG)
    assert "referenceNumber" not in b2["body"]


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


def test_dry_run_previews_exactly_what_live_would_do_without_writing():
    """A dry run performs the same READ-ONLY preflight as live -- so it reports
    skip / retry / would-post / would-draft truthfully -- and never creates,
    completes, or records anything."""
    shipped = {"/hddecorating/api/product/138VB5236WHTC": 1.0}
    with patch("app.tracking.get_finale_invoices", return_value={}), patch("app.tracking.record_events") as rec, \
         patch("app.tracking.record_finale_invoice") as rec_inv:
        clean = FakeFinale(shipped=shipped)
        out = push_finale_invoices([INV], PO_MAP, live=False, refs=REFS, client=clean)
        assert out["mode"] == "dry" and out["results"][0]["status"] == "built" and out["results"][0]["would"] == "posted"
        assert out["results"][0]["total"] == 21.42 and out["summary"] == {**out["summary"], "built": 1, "posted": 0}
        off = FakeFinale(shipped=shipped)
        out2 = push_finale_invoices([{**INV, "total_amount": 22.00}], PO_MAP, live=False, refs=REFS, client=off)
        assert out2["results"][0]["would"] == "draft"
        already = FakeFinale(invoices=[{"invoiceId": "9", "invoiceIdUser": "PO1-1", "statusId": "INVOICE_APPROVED"}], shipped=shipped)
        out3 = push_finale_invoices([INV], PO_MAP, live=False, refs=REFS, client=already)
        assert out3["results"][0]["status"] == "skipped_exists" and out3["summary"]["skipped_exists"] == 1
        unshipped = FakeFinale(shipped=None)
        out4 = push_finale_invoices([INV], PO_MAP, live=False, refs=REFS, client=unshipped)
        assert out4["results"][0]["status"] == "skipped_not_shipped"
    for c in (clean, off, already, unshipped):
        assert not [x for x in c.calls if x[0] in ("create", "complete", "complete_order")]   # reads only
    rec.assert_not_called(); rec_inv.assert_not_called()


def test_live_clean_invoice_is_created_and_posted_with_receipts():
    client = FakeFinale(shipped={"/hddecorating/api/product/138VB5236WHTC": 1.0})
    out, rec_inv, rec_ev = _live(client)
    r = out["results"][0]
    assert r["status"] == "posted" and r["invoice_id_user"] == "PO1-1" and r["qty_flag"] is None
    assert [c[0] for c in client.calls] == ["get_order", "create", "complete", "complete_order"]
    assert r["order_completed"] is True                                   # posted -> order completed
    assert client.calls[1][1]["invoiceItemList"][1]["amount"] == -1.04     # exact cents sent
    rec_inv.assert_called_once_with("T1", "PO1", "100407", "/hddecorating/api/invoice/100407", "PO1-1", "posted",
                                    created_by="API_KEY_U_BLINDS", finale_total=21.42, delta=0.0)
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
    with patch("app.finale.FinaleClient") as FC, patch("app.tracking.get_finale_invoices", return_value={}):
        FC.configured.return_value = True
        inst = FC.return_value
        inst.product_index.return_value = INDEX
        inst.get_order.return_value = {"orderId": "PO1", "invoiceUrlList": [], "shipmentUrlList": []}
        inst.order_invoices.return_value = []
        inst.shipment_qty_for_order.return_value = {"/hddecorating/api/product/138VB5236WHTC": 1.0}
        out = push_finale_invoices([INV], PO_MAP, live=False, refs=REFS)
    inst.product_index.assert_called_once()
    assert out["results"][0]["status"] == "built" and out["results"][0]["missing_products"] == []
    assert out["results"][0]["would"] == "posted"
    inst.create_invoice.assert_not_called(); inst.complete_invoice.assert_not_called()


def test_hollow_completed_order_reports_would_reopen_when_gate_off():
    """Completed + no invoice = not complete (Ritchie's rule). With auto_reopen off the
    engine says so once (would_reopen) and writes nothing; a completed order that DOES
    carry an invoice is simply 'exists'."""
    client = FakeFinale(shipped={"/hddecorating/api/product/138VB5236WHTC": 1.0})
    client._order = {**client._order, "statusId": "ORDER_COMPLETED"}
    out, rec_inv, rec_ev = _live(client)
    r = out["results"][0]
    assert r["status"] == "skipped_completed" and r["would_reopen"] is True
    assert "reopen" not in [c[0] for c in client.calls] and "create" not in [c[0] for c in client.calls]
    rec_inv.assert_not_called(); rec_ev.assert_not_called()
    done = FakeFinale(invoices=[{"invoiceId": "9", "invoiceIdUser": "PO1-1", "statusId": "INVOICE_APPROVED"}], shipped={"/hddecorating/api/product/138VB5236WHTC": 1.0})
    done._order = {**done._order, "statusId": "ORDER_COMPLETED"}
    out2, _, _ = _live(done)
    assert out2["results"][0]["status"] == "skipped_exists"


def test_hollow_completed_order_is_reopened_and_invoiced_when_gate_on():
    """auto_reopen on: edit -> lock, then the normal path -- shipped -> invoice posted ->
    order re-completed; unshipped -> left open for the warehouse and retried."""
    client = FakeFinale(shipped={"/hddecorating/api/product/138VB5236WHTC": 1.0})
    client._order = {**client._order, "statusId": "ORDER_COMPLETED"}
    out, rec_inv, rec_ev = _live(client, refs={"finale": {**REFS["finale"], "auto_reopen": True}})
    r = out["results"][0]
    assert r["status"] == "posted" and r["reopened"] is True and r["order_completed"] is True
    assert [c[0] for c in client.calls] == ["get_order", "reopen", "create", "complete", "complete_order"]
    unshipped = FakeFinale(shipped=None)
    unshipped._order = {**unshipped._order, "statusId": "ORDER_COMPLETED"}
    out2, rec_inv2, _ = _live(unshipped, refs={"finale": {**REFS["finale"], "auto_reopen": True}})
    assert out2["results"][0]["status"] == "skipped_not_shipped" and out2["results"][0]["reopened"] is True
    assert [c[0] for c in unshipped.calls] == ["get_order", "reopen"]      # reopened, nothing invoiced
    rec_inv2.assert_not_called()
    # a dry run never reopens, it reports
    dry = FakeFinale(shipped={"/hddecorating/api/product/138VB5236WHTC": 1.0})
    dry._order = {**dry._order, "statusId": "ORDER_COMPLETED"}
    with patch("app.tracking.get_finale_invoices", return_value={}):
        out3 = push_finale_invoices([INV], PO_MAP, live=False, refs={"finale": {**REFS["finale"], "auto_reopen": True}}, client=dry)
    r3 = out3["results"][0]
    assert r3["status"] == "built" and r3["would"] == "posted" and r3["would_reopen"] is True   # previews the live outcome
    assert "reopen" not in [c[0] for c in dry.calls]


def test_cap_counts_invoices_about_to_be_created_not_the_pending_set():
    """max_per_run refuses a run (nothing written) only when the rows that SURVIVE
    preflight exceed it; an Accepted 810 whose order has not shipped is waiting, not
    writing, and never pushes the poll into refusing."""
    inv2 = {**INV, "transaction_id": "T2", "source_document_id": "S2", "invoice_number": "INV2", "po_number": "PO2"}
    po_map = {**PO_MAP, "PO2": PO_MAP["PO1"]}
    class TwoOrders(FakeFinale):
        def __init__(self, shipped_by_po):
            super().__init__(shipped={}); self._by_po = shipped_by_po
        def get_order(self, oid):
            self.calls.append(("get_order", oid)); self._shipped = self._by_po[oid]
            return {"orderId": oid, "invoiceUrlList": [], "shipmentUrlList": []}
    both = TwoOrders({"PO1": {"/hddecorating/api/product/138VB5236WHTC": 1.0}, "PO2": {"/hddecorating/api/product/138VB5236WHTC": 1.0}})
    with patch("app.tracking.get_finale_invoices", return_value={}), patch("app.tracking.record_finale_invoice") as rec:
        out = push_finale_invoices([INV, inv2], po_map, live=True, refs=REFS, client=both, max_per_run=1)
    assert out["blocked"].startswith("2 invoices to create exceeds max_per_run 1")
    assert "create" not in [c[0] for c in both.calls] and rec.assert_not_called() is None
    assert [r["would"] for r in out["results"]] == ["posted", "posted"]           # the preview survives the refusal
    one = TwoOrders({"PO1": {"/hddecorating/api/product/138VB5236WHTC": 1.0}, "PO2": None})   # PO2 not shipped yet
    with patch("app.tracking.get_finale_invoices", return_value={}), patch("app.tracking.record_finale_invoice"), \
         patch("app.tracking.record_events"):
        out2 = push_finale_invoices([INV, inv2], po_map, live=True, refs=REFS, client=one, max_per_run=1)
    assert "blocked" not in out2 and [r["status"] for r in out2["results"]] == ["posted", "skipped_not_shipped"]
    with patch("app.tracking.get_finale_invoices", return_value={}):
        dry = push_finale_invoices([INV, inv2], po_map, live=False, refs=REFS, client=TwoOrders(both._by_po), max_per_run=1)
    assert dry["blocked"] and dry["mode"] == "dry"                                  # a dry run shows the refusal too


def _draft(by="API_KEY_U_BLINDS"):
    return {"invoiceId": "100450", "invoiceUrl": "/hddecorating/api/invoice/100450", "invoiceIdUser": "PO1-1",
            "statusId": "INVOICE_IN_PROCESS", "statusIdHistoryList": [{"statusId": None, "userLoginUrl": f"/hddecorating/api/userlogin/{by}"}]}


def test_lone_unreceipted_draft_is_adopted_not_duplicated():
    """A previous run created the draft but lost the post/receipt: instead of
    skipped_exists forever, adopt it -- post it (ours + clean), receipt it, complete
    the order. No second invoice is ever created."""
    shipped = {"/hddecorating/api/product/138VB5236WHTC": 1.0}
    ours = FakeFinale(invoices=[_draft()], shipped=shipped)
    out, rec_inv, _ = _live(ours)
    r = out["results"][0]
    assert r["status"] == "posted" and r["adopted"] == "PO1-1" and r["invoice_id_user"] == "PO1-1"
    assert [c[0] for c in ours.calls] == ["get_order", "complete", "complete_order"]        # no create
    assert rec_inv.call_args.args[-1] == "posted"
    # keyed by a person: adopt as a draft to review, never post someone else's draft
    theirs = FakeFinale(invoices=[_draft(by="edward.schiavon")], shipped=shipped)
    out2, rec2, _ = _live(theirs)
    assert out2["results"][0]["status"] == "draft" and "complete" not in [c[0] for c in theirs.calls]
    assert rec2.call_args.args[-1] == "draft"
    # a posted invoice, or more than one, is still skipped_exists
    posted = FakeFinale(invoices=[{**_draft(), "statusId": "INVOICE_APPROVED"}], shipped=shipped)
    assert _live(posted)[0]["results"][0]["status"] == "skipped_exists"
    two = FakeFinale(invoices=[_draft(), {**_draft(), "invoiceId": "100451"}], shipped=shipped)
    assert _live(two)[0]["results"][0]["status"] == "skipped_exists"
    # dry run previews the adoption and writes nothing
    dry = FakeFinale(invoices=[_draft()], shipped=shipped)
    with patch("app.tracking.get_finale_invoices", return_value={}):
        out3 = push_finale_invoices([INV], PO_MAP, live=False, refs=REFS, client=dry)
    assert out3["results"][0]["would"] == "posted" and out3["results"][0]["adopted"] == "PO1-1" and dry.calls == [("get_order", "PO1")]


HAND_INV = {"invoiceId": "100405", "invoiceUrl": "/hddecorating/api/invoice/100405", "invoiceIdUser": "PO1-1",
            "statusId": "INVOICE_APPROVED",
            "invoiceItemList": [{"invoiceItemTypeId": "INV_PROD_ITEM", "unitPrice": 20.0, "quantity": 1},
                                {"invoiceItemTypeId": "INV_PROMOTION_ADJ", "amount": -1.00},
                                {"invoiceItemTypeId": "INV_SALES_TAX", "amount": 2.47}],   # 21.47 vs 810 21.42
            "statusIdHistoryList": [{"statusId": None, "userLoginUrl": "/hddecorating/api/userlogin/edward.schiavon"},
                                    {"statusId": "INVOICE_APPROVED", "userLoginUrl": "/hddecorating/api/userlogin/edward.schiavon"}]}


def test_existing_hand_made_invoice_is_reconciled_and_receipted_as_external():
    """An order already invoiced by someone else is not an error and not 'missing':
    the engine reads that invoice -- who made it, its total, the delta vs the 810 --
    receipts it as 'external' (with the finale event, so the poll stops re-reading
    it) and never creates a second one. A dry run reports the same, writes nothing."""
    client = FakeFinale(invoices=[HAND_INV], shipped={"/hddecorating/api/product/138VB5236WHTC": 1.0})
    out, rec_inv, rec_ev = _live(client)
    r = out["results"][0]
    assert r["status"] == "skipped_exists" and "create" not in [c[0] for c in client.calls]
    assert r["external"] == {"invoice_id": "100405", "invoice_url": "/hddecorating/api/invoice/100405",
                             "invoice_id_user": "PO1-1", "created_by": "edward.schiavon", "finale_status": "posted",
                             "finale_total": 21.47, "delta": 0.05}
    rec_inv.assert_called_once_with("T1", "PO1", "100405", "/hddecorating/api/invoice/100405", "PO1-1", "external",
                                    created_by="edward.schiavon", finale_total=21.47, delta=0.05)
    rec_ev.assert_called_once_with(["T1"], "finale")
    dry = FakeFinale(invoices=[HAND_INV], shipped={"/hddecorating/api/product/138VB5236WHTC": 1.0})
    with patch("app.tracking.get_finale_invoices", return_value={}), \
         patch("app.tracking.record_finale_invoice") as rec2, patch("app.tracking.record_events") as ev2:
        out2 = push_finale_invoices([INV], PO_MAP, live=False, refs=REFS, client=dry)
    assert out2["results"][0]["external"]["delta"] == 0.05 and out2["results"][0]["status"] == "skipped_exists"
    rec2.assert_not_called(); ev2.assert_not_called()
    # an 'external' receipt short-circuits like ours: nothing is re-read
    client3 = FakeFinale(invoices=[HAND_INV])
    with patch("app.tracking.get_finale_invoices",
               return_value={"T1": {"invoice_id": "100405", "invoice_url": "/i", "invoice_id_user": "PO1-1", "status": "external"}}), \
         patch("app.tracking.record_finale_invoice") as rec3, patch("app.tracking.record_events") as ev3:
        out3 = push_finale_invoices([INV], PO_MAP, live=True, refs=REFS, client=client3)
    assert out3["results"][0]["status"] == "skipped_exists" and client3.calls == []
    rec3.assert_not_called(); ev3.assert_not_called()


def test_two_live_invoices_on_one_order_are_summed_and_named():
    other = {**HAND_INV, "invoiceId": "100406", "invoiceUrl": "/hddecorating/api/invoice/100406", "invoiceIdUser": "PO1-2",
             "invoiceItemList": [{"invoiceItemTypeId": "INV_PROD_ITEM", "unitPrice": 1.0, "quantity": 1}]}
    client = FakeFinale(invoices=[HAND_INV, other], shipped={"/hddecorating/api/product/138VB5236WHTC": 1.0})
    out, rec_inv, _ = _live(client)
    ext = out["results"][0]["external"]
    assert ext["invoice_id_user"] == "PO1-1, PO1-2" and ext["finale_total"] == 22.47 and ext["delta"] == 1.05
    assert rec_inv.call_args.args[5] == "external" and rec_inv.call_args.kwargs["finale_total"] == 22.47
