"""app.invoice_checks: HD's invoice rules (CMP Vendor Best Practice Document v9-12-2025),
and the digest's FYI section built on them (Ritchie, 2026-09-22). Rule fixtures use the
real invoices the review found by hand."""
from unittest.mock import patch

import pytest

from app import invoice_checks as ic


def _inv(channel="dropship", province="ON", gross=100.0, discounts=None, taxes=None, total=None,
         vendor="27006262", number="INV538000001", status="Accepted",
         created="2026-09-15T12:00:00Z", gst=None, transaction_id="tx-1"):
    """An invoice as the CRSTL cache holds it. discounts = [(code, amount)];
    taxes = [(code, amount)] -- SAC codes on DSD, TXI codes on dropship."""
    ac = [{"type": "Allowance", "code": c, "amount": a,
           "category": "discount" if c in ("I170", "C300") else "allowance"} for c, a in (discounts or [])]
    allowance = round(sum(a for c, a in (discounts or []) if c not in ("I170", "C300")), 2)
    discount = round(sum(a for c, a in (discounts or []) if c in ("I170", "C300")), 2)
    txi, sac_tax = [], 0.0
    for c, a in taxes or []:
        if channel == "dsd":
            ac.append({"type": "Charge", "code": c, "amount": a, "category": "tax"})
            sac_tax += a
        else:
            txi.append({"code": c, "amount": a})
    net = gross - allowance - discount
    return {
        "transaction_id": transaction_id, "invoice_number": number, "po_number": number[3:], "status": status,
        "trading_partner_flavor": "Direct Store Delivery (DSD)" if channel == "dsd" else "Dropship",
        "province": province, "subtotal": gross, "allowance_amount": allowance, "discount_amount": discount,
        "freight_amount": 0.0, "fee_amount": 0.0, "tax_amount": round(sac_tax, 2), "txi": txi,
        "allowances_charges": ac, "vendor_number": vendor, "created_at": created,
        "gst_registration": gst if gst is not None else ("836908962RT0001" if channel == "dsd" else ""),
        "total_amount": total if total is not None else round(net + sac_tax + sum(t["amount"] for t in txi), 2),
    }


def _good(channel, province, gross, number="INV538000001", **kw):
    """Discounts per the Backward Calculator and tax per HD's matrix."""
    tr, ibx = round(gross * 0.005, 2), round(gross * 0.0125, 2)
    met = round((gross - tr - ibx) * 0.035, 2)
    d = [("I170", tr), ("H000" if channel == "dsd" else "C300", ibx), ("E210", met)]
    if channel == "dsd":
        d.append(("H090", round(gross * 0.01, 2)))
    net = gross - sum(a for _, a in d)
    exp = ic.expected_tax(province, channel)
    if province == "QC":
        t = [("D360" if channel == "dsd" else "CG", round(net * 0.05, 2)),
             ("H680" if channel == "dsd" else "ST", round(net * 0.09975, 2))]
    else:
        t = [(next(iter(exp["codes"])), round(net * exp["rate"], 2))]
    return _inv(channel, province, gross, d, t, number=number, **kw)


def _keys(inv):
    return [i["issue"] for i in ic.check_invoice(inv)]


# ── rules ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("channel,province", [("dropship", "ON"), ("dropship", "NS"), ("dropship", "SK"),
                                              ("dropship", "BC"), ("dropship", "QC"), ("dsd", "ON"), ("dsd", "AB")])
def test_invoices_that_follow_hds_rules_raise_nothing(channel, province):
    assert _keys(_good(channel, province, 1234.56)) == []


def test_crstl_rounding_each_discount_is_not_flagged():
    """INV538770174 (Sep 11, ON): MET 5.48 against 5.51 by formula -- CRSTL rounds each
    discount on its own; a few cents is normal, not an error."""
    inv = _inv("dropship", "ON", 160.30, [("I170", 0.80), ("C300", 2.00), ("E210", 5.48)], [("VA", 19.76)])
    assert _keys(inv) == []


def test_saskatchewan_pst_is_flagged_once():
    """INV538909096 (Sep 18): GST 4.84 + ST 5.80 -- HD is PST-exempt (E995 on six like it)."""
    inv = _inv("dropship", "SK", 101.40, [("I170", 0.51), ("C300", 1.28), ("E210", 3.49)], [("CG", 4.84), ("ST", 5.80)])
    issues = ic.check_invoice(inv)
    assert [i["issue"] for i in issues] == ["tax_pst"]
    assert "HD is PST-exempt" in issues[0]["problem"] and issues[0]["outcome"] == ic.REJECT


def test_met_taken_on_gross_is_named():
    """INV40865972 (Vaughan, Sep 17): MET 450.71 = 3.5% of gross, not of the subtotal."""
    inv = _inv("dsd", "ON", 12877.36, [("I170", 64.39), ("H000", 160.97), ("E210", 450.71), ("H090", 128.77)],
               [("H770", 1569.43)])
    issues = ic.check_invoice(inv)
    assert [i["issue"] for i in issues] == ["discount_off:E210"]
    assert "calculated on gross" in issues[0]["problem"] and issues[0]["outcome"] == ic.UNDERPAID


def test_missing_and_doubled_discounts():
    only_ibx = _inv("dropship", "ON", 100.0, [("C300", 1.25)], [("VA", 12.84)])      # Aug 4 - Sep 1 pattern
    assert set(_keys(only_ibx)) >= {"discount_missing:I170", "discount_missing:E210"}
    twice = _inv("dropship", "BC", 100.0, [("I170", 0.50), ("I170", 0.50), ("C300", 1.25), ("E210", 3.44)],
                 [("CG", 4.72)])                                                      # Sep 9-10 pattern
    assert "discount_twice:I170" in _keys(twice)


def test_tax_under_the_wrong_code_no_tax_and_quebec_without_qst():
    h850 = _inv("dsd", "ON", 1000.0, [("I170", 5.0), ("H000", 12.5), ("E210", 34.39), ("H090", 10.0)], [("H850", 121.29)])
    assert "tax_code" in _keys(h850)                                                  # Jul 17 Vaughan pattern
    untaxed = _inv("dsd", "AB", 1000.0, [("I170", 5.0), ("H000", 12.5), ("E210", 34.39), ("H090", 10.0)], [])
    assert _keys(untaxed) == ["tax_none"]                                             # Mar-Jul DSD pattern
    qc = _inv("dropship", "QC", 100.0, [("I170", 0.5), ("C300", 1.25), ("E210", 3.44)], [("CG", 4.74)])
    assert "tax_qst_missing" in _keys(qc)


def test_header_rules():
    bad_vendor = _good("dropship", "ON", 100.0, vendor="70009708")                    # Aug 4-18 pattern
    assert _keys(bad_vendor) == ["vendor_number"]
    assert _keys(_good("dropship", "ON", 100.0, number="INV538596153-1")) == ["invoice_number"]
    off = _good("dropship", "ON", 100.0)
    off["total_amount"] += 1.00
    assert _keys(off) == ["total_off"]


def test_wording_is_an_fyi_not_an_alarm():
    """Ritchie: an FYI for accounting to look into, never alarming -- 'may', never 'will'."""
    for outcome in (ic.REJECT, ic.PARK, ic.UNDERPAID, ic.TAX_RISK, ic.NO_TAX):
        assert "may" in outcome.lower()
        assert not any(w in outcome.lower() for w in ("will", "rejected", "error", "fail"))


def test_check_all_reads_what_hd_has():
    """The latest ACCEPTED version per invoice number, on/after the floor; a later Draft
    means a correction exists that was never sent."""
    old_bad = _good("dropship", "SK", 100.0, number="INV1", created="2026-09-12T10:00:00Z", transaction_id="a")
    old_bad["txi"].append({"code": "ST", "amount": 5.66})
    fixed = _good("dropship", "SK", 100.0, number="INV1", created="2026-09-14T10:00:00Z", transaction_id="b")
    before_floor = _good("dropship", "ON", 100.0, number="INV2", vendor="70009708", created="2026-09-01T10:00:00Z")
    unsent_fix = _good("dropship", "BC", 100.0, number="INV3", created="2026-09-12T10:00:00Z", transaction_id="c")
    unsent_fix["txi"].append({"code": "ST", "amount": 6.66})
    draft = dict(unsent_fix, status="Draft", created_at="2026-09-15T10:00:00Z", transaction_id="d")
    r = ic.check_all([old_bad, fixed, before_floor, unsent_fix, draft], "2026-09-11")
    assert set(r["results"]) == {"INV3"}
    assert r["results"]["INV3"]["draft_at"] == "2026-09-15T10:00:00Z"
    assert r["checked"] == 2 and r["dropship_without_gst"] == 2


# ── the digest section ─────────────────────────────────────────────────────────

def _sk_pst(number="INV538909096", created="2026-09-18T15:00:00Z"):
    inv = _good("dropship", "SK", 101.40, number=number, created=created, transaction_id=number)
    inv["txi"].append({"code": "ST", "amount": 5.80})
    inv["total_amount"] = round(inv["total_amount"] + 5.80, 2)
    return inv


@pytest.fixture
def checks_on():
    from app import tracking
    tracking.init_db()
    with patch("app.accounting._invoice_checks_config", return_value={"enabled": True, "created_after": "2026-09-11"}):
        yield


def test_section_new_then_noted_then_cleared(checks_on):
    from app import accounting, tracking
    from app.crstl_cache import _cache
    bad = _sk_pst()
    with patch.dict(_cache, {"invoices": [bad], "status": "ok"}):
        first = accounting._invoice_check_data()
        assert [r["invoice_number"] for r in first["rows"]] == ["INV538909096"] and first["rows"][0]["new"]
        assert accounting._invoice_check_data()["rows"][0]["new"]          # a preview writes nothing
        tracking.sync_invoice_checks(first["current"])                     # the digest went out
        again = accounting._invoice_check_data()
        assert not again["rows"][0]["new"] and again["rows"][0]["first_seen"]
    fixed = _good("dropship", "SK", 101.40, number="INV538909096", created="2026-09-19T15:00:00Z", transaction_id="t2")
    with patch.dict(_cache, {"invoices": [bad, fixed], "status": "ok"}):
        cleared = accounting._invoice_check_data()
        assert cleared["rows"] == [] and cleared["cleared"] == ["INV538909096"]


def test_failed_data_load_never_clears_findings(checks_on):
    from app import accounting, tracking
    from app.crstl_cache import _cache
    with patch.dict(_cache, {"invoices": [_sk_pst()], "status": "ok"}):
        tracking.sync_invoice_checks(accounting._invoice_check_data()["current"])
    with patch.dict(_cache, {"invoices": [], "status": "error: CRSTL down"}):
        chk = accounting._invoice_check_data()
    assert chk["unavailable"] and chk["current"] is None
    assert "did not load" in accounting._invoice_check_html(chk)
    assert all(r["resolved_at"] is None for r in tracking.get_invoice_checks().values())


def test_digest_carries_the_fyi_at_the_bottom_and_records_after_sending(checks_on, monkeypatch):
    from app import tracking
    from app.accounting import _send_daily_digest
    from app.crstl_cache import _cache
    monkeypatch.setenv("MAIL_RECIPIENTS", "accounting@example.com")
    with patch.dict(_cache, {"invoices": [_sk_pst()], "status": "ok"}), patch("app.accounting.send_mail") as mail:
        result = _send_daily_digest()
    body, subject = mail.call_args.kwargs["body_html"], mail.call_args.kwargs["subject"]
    section = body.split("invoices to watch in HD's portal")[-1]
    assert "INV538909096" in section and "May be returned by HD" in section
    assert body.rstrip().endswith(section.rstrip())                        # last thing in the email
    assert "#b32020" not in section and "watch" not in subject             # calm: no red, subject untouched
    assert result["to_watch"] == 1
    assert list(tracking.get_invoice_checks()) == ["INV538909096:tax_pst"]


def test_digest_send_failure_records_nothing(checks_on, monkeypatch):
    from app import tracking
    from app.accounting import _send_daily_digest
    from app.crstl_cache import _cache
    monkeypatch.setenv("MAIL_RECIPIENTS", "accounting@example.com")
    with patch.dict(_cache, {"invoices": [_sk_pst()], "status": "ok"}), \
         patch("app.accounting.send_mail", side_effect=RuntimeError("smtp down")):
        with pytest.raises(RuntimeError):
            _send_daily_digest()
    assert tracking.get_invoice_checks() == {}


def test_section_is_absent_while_disabled(monkeypatch):
    from app import tracking
    from app.accounting import _send_daily_digest
    from app.crstl_cache import _cache
    tracking.init_db()
    monkeypatch.setenv("MAIL_RECIPIENTS", "accounting@example.com")
    with patch("app.accounting._invoice_checks_config", return_value={"enabled": False, "created_after": "2026-09-11"}), \
         patch.dict(_cache, {"invoices": [_sk_pst()], "status": "ok"}), patch("app.accounting.send_mail") as mail:
        _send_daily_digest()
    assert "invoices to watch" not in mail.call_args.kwargs["body_html"]
    assert tracking.get_invoice_checks() == {}


def test_preview_endpoint_is_read_only(checks_on):
    from fastapi.testclient import TestClient
    from app import tracking
    from app.crstl_cache import _cache
    from app.main import app
    with patch.dict(_cache, {"invoices": [_sk_pst()], "status": "ok"}):
        r = TestClient(app).get("/api/invoice-checks")
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is True and [row["invoice_number"] for row in data["rows"]] == ["INV538909096"]
    assert "current" not in data and tracking.get_invoice_checks() == {}


def test_real_config_reaches_the_digest():
    """No stand-in: the section's settings must come through load_refs(). They once did
    not, and the live preview checked all history instead of from Sep 11."""
    from app import accounting
    cfg = accounting._invoice_checks_config()
    assert cfg.get("created_after") == "2026-09-11"
    assert cfg.get("enabled") is True         # Ritchie, 2026-09-22, after the preview email


def test_no_start_date_checks_nothing():
    from app import accounting, tracking
    from app.crstl_cache import _cache
    tracking.init_db()
    with patch("app.accounting._invoice_checks_config", return_value={"enabled": True}), \
         patch.dict(_cache, {"invoices": [_sk_pst()], "status": "ok"}):
        chk = accounting._invoice_check_data()
    assert chk["unavailable"] and chk["rows"] == [] and chk["current"] is None
    assert "no start date" in accounting._invoice_check_html(chk)


# ── an invoice CRSTL changed after we pushed it downstream ─────────────────────

def test_changed_after_push_is_an_expected_chargeback_not_an_edit():
    """CRSTL edits an ACCEPTED invoice in place (six SK invoices lost their PST on
    2026-09-22), but HD keeps what it accepted and it can't be re-sent: HD charges the
    difference back. Accounting sees NetSuite, not CRSTL, so the row speaks of that."""
    inv = _good("dropship", "SK", 101.40, number="INV538909096", transaction_id="tx-sk")
    inv["total_amount"] = 101.56
    assert ic.changed_after_push(inv, {}) == []                                  # never pushed: nothing to say
    assert ic.changed_after_push(inv, {"netsuite": {"hd_total": 101.56}}) == []   # unchanged since the push
    one = ic.changed_after_push(inv, {"netsuite": {"hd_total": 107.36}}, netsuite_total=107.36)
    assert [i["issue"] for i in one] == ["changed_after_push"]
    assert one[0]["problem"].startswith("HD was billed $107.36; the correct amount is $101.56")
    assert "chargeback of about $5.80" in one[0]["outcome"] and "match it to this invoice" in one[0]["outcome"]
    assert "not checked" not in one[0]["outcome"]
    unread = ic.changed_after_push(inv, {"netsuite": {"hd_total": 107.36}})
    assert unread[0]["outcome"].endswith("(NetSuite not checked today)")
    for x in one + unread:
        assert "draft" not in (x["problem"] + x["outcome"]).lower() and "crstl" not in (x["problem"] + x["outcome"]).lower()


def test_netsuite_already_corrected_says_not_to_apply_the_chargeback_twice():
    """INV538740348: HD billed $60.53 with PST, NetSuite already holds $55.25 without it."""
    inv = _good("dropship", "SK", 55.50, number="INV538740348", transaction_id="tx-old")
    inv["total_amount"] = 55.35
    x = ic.changed_after_push(inv, {"netsuite": {"hd_total": 60.53}}, netsuite_total=55.25)[0]
    assert "NetSuite already has $55.25" in x["problem"]
    assert "chargeback of about $5.18" in x["outcome"] and "don't reduce this entry again" in x["outcome"]


def test_under_billed_says_hd_will_not_pay_the_difference():
    inv = _good("dropship", "ON", 100.0, transaction_id="tx-u")
    x = ic.changed_after_push(inv, {"netsuite": {"hd_total": inv["total_amount"] - 3}})[0]
    assert "more)" in x["problem"] and "claimed in HD's portal" in x["outcome"]


def test_a_cent_of_rounding_is_not_a_change():
    inv = _good("dropship", "ON", 100.0, transaction_id="tx-1")
    assert ic.changed_after_push(inv, {"netsuite": {"hd_total": inv["total_amount"] + 0.01}}) == []
    assert ic.changed_after_push(inv, {"netsuite": {"hd_total": inv["total_amount"] + 0.02}}) != []


def test_digest_reports_a_changed_invoice_even_from_before_the_floor(checks_on):
    """The floor bounds HD's rules; our own books are checked on every pushed invoice,
    however old -- INV538740348 (Sep 9) is before the Sep 11 floor and still counts. It
    reads NetSuite's real entry and lists the row under chargebacks, not 'nothing to do'."""
    from app import accounting, tracking
    from app.crstl_cache import _cache
    old = _good("dropship", "SK", 55.50, number="INV538740348",
                created="2026-09-09T15:09:00Z", transaction_id="tx-old")
    old["total_amount"] = 55.35
    tracking.record_push_snapshot("tx-old", "INV538740348", "netsuite", 60.53)
    with patch.dict(_cache, {"invoices": [old], "status": "ok"}), \
         patch("app.accounting._netsuite_reader", return_value=object()), \
         patch("app.accounting._netsuite_total", return_value=55.25) as ns:
        chk = accounting._invoice_check_data()
    ns.assert_called_once()
    row = next(r for r in chk["rows"] if r["invoice_number"] == "INV538740348")
    assert "NetSuite already has $55.25" in row["problems"][0]
    assert "don't reduce this entry again" in row["outcomes"][0]
    html = accounting._invoice_check_html(chk)
    assert "HD chargebacks to expect" in html and "Nothing to do now" not in html
    assert "draft" not in html.lower()


def test_an_expected_chargeback_is_reported_once_then_never_repeated(checks_on):
    """Shown in the first digest that goes out, then silent -- but still tracked, so it
    is not marked resolved and can't come back as 'new'."""
    from app import accounting, tracking
    from app.crstl_cache import _cache
    inv = _good("dropship", "SK", 101.40, number="INV538909096", transaction_id="tx-sk")
    tracking.record_push_snapshot("tx-sk", "INV538909096", "netsuite", inv["total_amount"] + 5.80)

    def check():
        with patch.dict(_cache, {"invoices": [inv], "status": "ok"}), \
             patch("app.accounting._netsuite_reader", return_value=False):
            return accounting._invoice_check_data()
    first = check()
    assert [r["invoice_number"] for r in first["rows"]] == ["INV538909096"]
    assert check()["rows"]                                   # digest not sent yet: still shown
    tracking.sync_invoice_checks(first["current"])           # the digest showing it went out
    for _ in range(2):
        again = check()
        assert again["rows"] == [] and again["cleared"] == []
        assert "INV538909096:changed_after_push" in {c["key"] for c in again["current"]}
        tracking.sync_invoice_checks(again["current"])
    assert "HD chargebacks to expect" not in accounting._invoice_check_html(again)


def test_met_off_its_formula_is_reported_once_other_rules_repeat():
    from app import invoice_checks as ic2
    assert ic2.reported_once("discount_off:E210") and ic2.reported_once("changed_after_push")
    assert not ic2.reported_once("discount_off:I170") and not ic2.reported_once("hd_reject:abc")


def test_snapshot_survives_a_repush_and_reads_back_per_target():
    from app import tracking
    tracking.init_db()
    tracking.record_push_snapshot("tx-9", "INV9", "netsuite", 100.00)
    tracking.record_push_snapshot("tx-9", "INV9", "finale", 100.00)
    tracking.record_push_snapshot("tx-9", "INV9", "netsuite", 95.00)     # pushed again, newer value wins
    snaps = tracking.get_push_snapshots(["tx-9", "tx-absent"])
    assert snaps["tx-9"]["netsuite"]["hd_total"] == 95.00
    assert snaps["tx-9"]["finale"]["hd_total"] == 100.00
    assert "tx-absent" not in snaps
    tracking.record_push_snapshot("tx-10", "INV10", "netsuite", None)    # nothing to record
    assert "tx-10" not in tracking.get_push_snapshots(["tx-10"])
