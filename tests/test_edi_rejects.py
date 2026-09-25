"""HD's 864 error messages (app.edi_rejects) and the alert rows they become
(app.alerts.find_edi_rejected): HD's own wording, collapsed repeats, a PO link when
the rejected document can be traced back to its 850, and a go-live floor so switching
the watcher on never reports the backlog.

The payloads below are verbatim from CRSTL (read 2026-09-24), both layouts HD uses.
"""
from unittest.mock import MagicMock

from app import edi_rejects
from app.alerts import EVENT_ISSUES, after_go_live, find_edi_rejected, run_alerts

GO_LIVE = "2026-09-24"


def detail(*lines: str) -> dict:
    return {"metadata": {"document_type": "864"},
            "file": {"generic_json_edi": {"heading": {"description": "ASN967125720260824133030"},
                                          "detail": {"message_identification_loop": [
                                              {"message_text": [{"free_form_message_text": t} for t in lines]}]}}}}


ASN_REJECT = detail(
    "HOME DEPOT EDI ASN REJECT ERRORS EDIT DATE: 2026-08-24",
    "EDIT TIME:13:30:29SENDER ID: CRLJL39BU3_CRLJL39BU3",
    "THE FOLLOWING ASN TRANSACTION(S) HAVE BEEN REJECTED DUE TO",
    "VIOLATION(S) OF HOME DEPOT MAPPING SPECIFICATIONS.",
    "CONTACT OR CONTACT VENDOR PROGRAMS AT VENDOR_PROGRAMS@HOMEDEPOT.COM",
    "ASN  NUMBER          E R R O R M E S S A G E",
    "ASN9671257 E114-ASN rejected due to duplicate ASN #",
    "ASN9671257 E712-ASN failed due to duplicate UCC128 code send --UCC128=00006279490210115647",
    "ASN9671257 E712-ASN failed due to duplicate UCC128 code send --UCC128=00006279490210115654",
)
INVOICE_REJECT = detail(
    "HOME DEPOT EDI INVOICE ERRORS EDIT DATE:2026-07-17",
    "EDIT TIME:13:37:05 SENDER ID:CRLJL39BU3_CRLJL39BU3",
    "THE FOLLOWING INVOICES HAVE BEEN REJECTED DUE TO BUSINESS RULE",
    "INVOICE NUMBER   E R R O R M E S S A G E",
    "INV40851907 ED44 - Cannot charge tax SAC code H680 QST without also charging D360 GST",
)
# HD's other invoice layout: a letter, the error on the last line, no column header.
INVOICE_LETTER = detail(
    "HOME DEPOT EDI INVOICE ERRORS EDIT DATE: 2026-03-27",
    "Hi Canadian Supplier-THD Canada sends an EDI 864-Error Message for each invoice which fails to match specific PO business rules.",
    "https://my.directcommerce.com/Login.jsp?customer=homedepotca",
    "Thank you!Canada Merch Payables",
    "INV9448098 E332-AP Vendor number starting with 25,26 or 27 required, retransmit",
)


def test_parse_keeps_hd_wording_and_ignores_the_boilerplate():
    p = edi_rejects.parse_864(ASN_REJECT)
    assert p["kind"] == edi_rejects.ASN and list(p["documents"]) == ["ASN9671257"]
    assert p["documents"]["ASN9671257"][0] == "E114-ASN rejected due to duplicate ASN #"
    assert len(p["documents"]["ASN9671257"]) == 3            # the column header is not an error
    assert edi_rejects.parse_864(INVOICE_REJECT)["kind"] == edi_rejects.INVOICE
    letter = edi_rejects.parse_864(INVOICE_LETTER)
    assert letter["kind"] == edi_rejects.INVOICE and list(letter["documents"]) == ["INV9448098"]
    assert edi_rejects.parse_864(None) == {"kind": "", "documents": {}}


def test_note_counts_repeats_instead_of_repeating_them():
    note = edi_rejects.reject_note(edi_rejects.parse_864(ASN_REJECT)["documents"]["ASN9671257"])
    assert note.startswith("E114-ASN rejected due to duplicate ASN #; E712-ASN failed due to duplicate")
    assert "(×2)" in note and note.count("E712") == 1
    long = edi_rejects.reject_note(["E999-" + "x" * 200])
    assert len(long) <= 91 and long.endswith("…")
    many = edi_rejects.reject_note([f"E{i}00-message {i}" for i in range(5)])
    assert many.endswith("+2 more")


def rec(tid="864a", created="2026-09-24T18:30:00.000Z", det=ASN_REJECT):
    return {"id": tid, "created_at": created, "detail": det}


def test_rows_link_to_the_po_and_name_the_rejected_document():
    rows = find_edi_rejected([rec()], {"ASN9671257": {"po_number": "4722011", "id": "850id"}},
                             go_live_after=GO_LIVE)
    assert len(rows) == 1
    r = rows[0]
    assert r["issue"] == "asn_rejected" and r["key"] == "asn_rejected:864a:ASN9671257"
    assert r["po_number"] == "4722011"                       # the PO, so the alert reads like the others
    assert r["url"] == "https://omnicrstl.web.app/edi/purchase-order/view/850id/850id"
    assert r["note"].startswith("ASN9671257 24 Sep 14:30 ET — E114-ASN rejected")   # 18:30Z is 14:30 ET
    # No 856/810 in CRSTL for it: still reported, labelled by the document itself.
    bare = find_edi_rejected([rec()], {}, go_live_after=GO_LIVE)[0]
    assert bare["po_number"] == "ASN9671257" and bare["url"] == ""
    # An invoice reject is accounting's: the warehouse alerts skip it (it goes to the
    # digest instead), and it is only produced when asked for explicitly.
    assert find_edi_rejected([rec(det=INVOICE_REJECT)], {}, go_live_after=GO_LIVE) == []
    inv = find_edi_rejected([rec(det=INVOICE_REJECT)], {}, go_live_after=GO_LIVE, invoice_rejects=True)[0]
    assert inv["issue"] == "invoice_rejected" and "ED44" in inv["note"]


def test_go_live_floor_is_the_864s_own_et_date():
    older = rec(created="2026-08-24T22:23:58.926Z")
    assert find_edi_rejected([older], {}, go_live_after=GO_LIVE) == []
    assert find_edi_rejected([rec(), older], {}, go_live_after=GO_LIVE)[0]["key"].endswith("ASN9671257")
    # No date configured reports nothing at all -- the backlog is never in scope.
    assert find_edi_rejected([rec()], {}, go_live_after=None) == []
    # 20:30 ET on go-live day is 00:30Z the NEXT day: the floor is the Toronto date.
    assert find_edi_rejected([rec(created="2026-09-25T00:30:00.000Z")], {}, go_live_after=GO_LIVE)
    assert after_go_live("2026-09-24T03:30:00.000Z", GO_LIVE) is False      # 23:30 ET on the 23rd
    assert after_go_live(None, GO_LIVE) is False and after_go_live("2026-09-24T18:00:00Z", None) is False


def test_an_unreadable_864_is_reported_rather_than_dropped():
    """CRSTL answers 400 for a document it has no mapping for (fetch_rejections then
    passes detail=None). Going quiet on a rejection is the failure this watches for."""
    rows = find_edi_rejected([rec(tid="6a8cc47e6fe6604da76f0314", det=None)], {}, go_live_after=GO_LIVE)
    assert rows[0]["issue"] == "edi_rejected" and rows[0]["key"] == "edi_rejected:6a8cc47e6fe6604da76f0314"
    assert rows[0]["po_number"] == "864 6f0314" and "no readable error text" in rows[0]["note"]


def test_emailed_once_then_closed_so_it_never_sits_still_open(tmp_path, monkeypatch):
    from app import tracking
    monkeypatch.setenv("TRACKING_DB", str(tmp_path / "t.db")); tracking.init_db()
    send = MagicMock()
    cfg = {"edi_reject_go_live_after": GO_LIVE}
    kw = dict(config=cfg, live=True, recipients=["order.alerts@x"], send=send,
              rejections=[rec()], doc_sources={"ASN9671257": {"po_number": "4722011", "id": "850id"}})
    first = run_alerts([], set(), {}, **kw)
    assert first["sent"] and send.call_count == 1
    assert send.call_args.kwargs["subject"] == "Order alert: HD rejected the ASN — 4722011"
    assert "correct and retransmit under the SAME ASN number (BSN02)" in send.call_args.kwargs["body_html"]
    # Same 864 next run: not re-sent, and not carried as "still open" either.
    second = run_alerts([], set(), {}, **kw)
    assert second["summary"] == {"found": 1, "new": 0, "still_open": 0, "resolved": 0}
    assert send.call_count == 1 and not tracking.open_alert_receipts()
    # A FRESH 864 for the same document is news again -- HD rejected it a second time.
    again = run_alerts([], set(), {}, **{**kw, "rejections": [rec(tid="864b")]})
    assert again["sent"] and send.call_count == 2
    assert all(i in EVENT_ISSUES for i in ("asn_rejected", "invoice_rejected", "edi_rejected"))


def test_no_rejections_configured_is_a_quiet_run(tmp_path, monkeypatch):
    from app import tracking
    monkeypatch.setenv("TRACKING_DB", str(tmp_path / "t.db")); tracking.init_db()
    send = MagicMock()
    out = run_alerts([], set(), {}, config={}, live=True, recipients=["order.alerts@x"], send=send,
                     rejections=[rec()])
    assert out["summary"]["found"] == 0 and send.call_count == 0


# --- the digest side: HD's verdict as an invoice-check issue -------------------------

def inv(num="INV40856494", po="40856494", created="2026-09-24T14:00:00Z", status="Accepted"):
    return {"invoice_number": num, "po_number": po, "created_at": created, "status": status,
            "trading_partner_flavor": "Direct Store Delivery (DSD)", "province": "ON"}


REJECTED_ED44 = {"INV40856494": [{"id": "864x", "at": "2026-09-24T19:07:00.000Z",
                                  "errors": ["ED44 - Cannot charge tax SAC code H680 QST without also charging D360 GST"]}]}


def hd_rows(result, num="INV40856494"):
    return [i for i in result["results"].get(num, {}).get("issues", []) if i["issue"].startswith("hd_reject")]


def test_digest_states_hd_rejected_it_and_what_to_do():
    from app import invoice_checks as ic
    rows = hd_rows(ic.check_all([inv()], "2026-09-11", rejections=REJECTED_ED44))
    assert len(rows) == 1 and rows[0]["issue"] == "hd_reject:864x"
    assert rows[0]["problem"] == ("HD returned it on 24 Sep: ED44 - Cannot charge tax SAC code "
                                 "H680 QST without also charging D360 GST")
    assert rows[0]["outcome"] == ic.RETURNED == "Correct and resend the same invoice number"


def test_a_rejection_clears_only_when_the_po_is_billed_again_and_accepted():
    from app import invoice_checks as ic
    accepted_later = inv(num="INV40856494B", created="2026-09-25T13:00:00Z")
    assert hd_rows(ic.check_all([inv(), accepted_later], "2026-09-11", rejections=REJECTED_ED44)) == []
    # A DRAFT replacement does not clear it: that is the stall these six all died of.
    draft = inv(created="2026-09-25T13:00:00Z", status="Draft")
    assert hd_rows(ic.check_all([inv(), draft], "2026-09-11", rejections=REJECTED_ED44))
    # Nor does an accepted invoice on that PO from BEFORE the rejection (the rejected one).
    earlier = inv(num="INV40856494A", created="2026-09-24T13:00:00Z")
    assert hd_rows(ic.check_all([inv(), earlier], "2026-09-11", rejections=REJECTED_ED44))


def test_hd_rejects_an_invoice_older_than_the_rules_floor_still_reported():
    """The floor governs which invoices we CHECK; HD's verdict is news whenever it lands."""
    from app import invoice_checks as ic
    old = inv(created="2026-08-01T14:00:00Z")
    assert hd_rows(ic.check_all([old], "2026-09-11", rejections=REJECTED_ED44))


def test_digest_reports_no_rejections_and_an_unreadable_feed_rather_than_silence():
    from app import accounting
    assert accounting._hd_rejections("") == ({}, None)      # no floor configured = nothing read
    base = {"unavailable": False, "rows": [], "cleared": [], "sent_since_last": 3,
            "rejects_after": "2026-09-24", "returned": [], "rejects_error": None}
    assert "no rejections from HD since 2026-09-24" in accounting._invoice_check_html(base)
    hurt = accounting._invoice_check_html({**base, "rejects_error": "CRSTL 500"})
    assert "could not all be read today" in hurt and "CRSTL 500" in hurt
    listed = accounting._invoice_check_html({**base, "returned": ["INV40856494"],
                                             "rows": [{"invoice_number": "INV40856494", "po_number": "40856494",
                                                       "channel": "DSD", "province": "ON", "sent": "2026-09-24",
                                                       "total": 23193.72, "problems": ["HD returned it on 24 Sep: ED44"],
                                                       "outcomes": ["Correct and resend the same invoice number"],
                                                       "new": True, "first_seen": None, "issues": []}]})
    assert "HD has returned 1 invoice</strong> (INV40856494)" in listed


# --- an ASN HD rejected must not close the "ASN not sent" alert ----------------------

def test_a_rejected_asn_does_not_resolve_the_asn_missing_alert(tmp_path, monkeypatch):
    """CRSTL calls a rejected 856 "Accepted" (verified on all 5 HD rejected), so without
    this the alert reports an all-clear for a shipment HD has no receipt for."""
    from app import tracking
    from app.alerts import resolved_asn_missing
    monkeypatch.setenv("TRACKING_DB", str(tmp_path / "t.db")); tracking.init_db()
    open_receipt = [{"key": "asn_missing:1", "po_number": "4722011", "issue": "asn_missing",
                     "sent_at": "2026-09-24T14:00:00+00:00", "resolved_at": None}]
    # An 856 exists and CRSTL says sent -> normally resolved...
    assert resolved_asn_missing(open_receipt, {"4722011"}) == open_receipt
    # ...but not once HD has rejected that ASN.
    assert resolved_asn_missing(open_receipt, {"4722011"}, frozenset(), {"4722011"}) == []
    # A voided label still resolves: cancelled is cancelled.
    assert resolved_asn_missing(open_receipt, set(), {"asn_missing:1"}, {"4722011"}) == open_receipt


def test_run_alerts_keeps_the_po_open_and_says_why(tmp_path, monkeypatch):
    from app import tracking
    monkeypatch.setenv("TRACKING_DB", str(tmp_path / "t.db")); tracking.init_db()
    send = MagicMock()
    tracking.record_alert("asn_missing:1", "4722011", "asn_missing")      # alerted earlier
    cfg = {"edi_reject_go_live_after": GO_LIVE, "asn_missing_after_minutes": 60}
    out = run_alerts([], {"4722011"}, {}, config=cfg, live=True, recipients=["order.alerts@x"], send=send,
                     rejections=[rec()], doc_sources={"ASN9671257": {"po_number": "4722011", "id": "850id"}})
    assert out["resolved"] == []                                          # NOT closed by the rejected 856
    assert [r["po_number"] for r in out["still_open"]] == ["4722011"]
    assert out["sent"] and "HD rejected the ASN" in send.call_args.kwargs["subject"]
    assert [r["key"] for r in tracking.open_alert_receipts()] == ["asn_missing:1"]
