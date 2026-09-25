"""HD's invoice rules, checked against every 810 we sent -- so accounting sees in the
daily digest which invoices HD is likely to reject, park or charge back, and can watch
for them in HD's CMP portal, instead of finding out weeks later (Ritchie, 2026-09-22).

Sources: HD's CMP Vendor Best Practice Document v9-12-2025 (reference/) -- VAT matrix
p.23, reject codes pp.8-14 -- and the discount math of the Backward Calculator V5.

CRSTL computes each discount separately and rounds, while NetSuite and Finale apply the
flat 6.18875% / 5.18875%, so a few cents apart is normal. Amounts are compared with a
tolerance that passes that rounding but catches a wrong basis: MET taken on gross is
1.8% off the right MET; CRSTL's rounding stays under 1%.

Pure functions: no I/O. The digest (app.accounting) feeds in the cached invoices and
keeps the first-seen / cleared record in tracking.db.
"""
from __future__ import annotations

import re
from datetime import datetime

GST_ONLY = {"AB", "BC", "MB", "SK", "NT", "NU", "YT"}
HST_RATES = {"ON": 0.13, "NB": 0.15, "NL": 0.15, "PE": 0.15, "NS": 0.14}

# What HD may do with an invoice like this. Worded as an FYI for accounting to keep an
# eye on in HD's portal, not an alarm (Ritchie, 2026-09-22): "may", never "will".
# HD's own verdict, not a prediction: its 864 error message names the invoice and the
# reason. Stated plainly (no "may") because it has already happened -- but still an FYI
# in the same muted section, not an alarm. The wording is HD's own rule for a rejected
# invoice (CMP doc: fix and retransmit the SAME number; a new number is a duplicate and
# is deleted). Clears when that PO is billed again and the new invoice is accepted.
RETURNED = "Correct and resend the same invoice number"
REJECT = "May be returned by HD"
PARK = "May be held for review"
UNDERPAID = "HD may pay slightly less"
TAX_RISK = "HD may not pay the tax"
NO_TAX = "Tax may need to be claimed later"

# (code, name, rate, basis): basis "gross" = the invoice lines; "subtotal" = gross
# less trade and IBX (MET only). Dropship drops RDC and freight.
DISCOUNTS = {
    "dsd": [("I170", "Trade", 0.005, "gross"), ("H000", "IBX", 0.0125, "gross"),
            ("E210", "MET", 0.035, "subtotal"), ("H090", "RDC", 0.01, "gross")],
    "dropship": [("I170", "Trade", 0.005, "gross"), ("C300", "IBX", 0.0125, "gross"),
                 ("E210", "MET", 0.035, "subtotal")],
}


def channel_of(inv: dict) -> str | None:
    flavor = str(inv.get("trading_partner_flavor") or "")
    if flavor.startswith("Direct Store"):
        return "dsd"
    if flavor == "Dropship":
        return "dropship"
    return None


def expected_tax(province: str, channel: str) -> dict | None:
    """HD's VAT matrix (p.23): the rate on net and the codes allowed. DSD sends SAC
    codes; dropship sends TXI codes (CRSTL maps them to SAC on the way out)."""
    p = (province or "").upper()
    sac = channel == "dsd"
    if p in GST_ONLY:
        return {"rate": 0.05, "codes": {"D360"} if sac else {"CG"}, "label": "GST 5%"}
    if p in HST_RATES:
        r = HST_RATES[p]
        return {"rate": r, "codes": {"H770"} if sac else {"VA"}, "label": f"HST {r:.0%}"}
    if p == "QC":
        return {"rate": 0.14975, "codes": {"D360", "H680"} if sac else {"CG", "ST"},
                "label": "GST 5% + QST 9.975%"}
    return None


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _issue(key: str, problem: str, outcome: str) -> dict:
    return {"issue": key, "problem": problem, "outcome": outcome}


def check_invoice(inv: dict) -> list[dict]:
    """Every rule this invoice breaks, in plain words, with HD's likely response."""
    ch = channel_of(inv)
    gross = round(float(inv.get("subtotal") or 0), 2)
    if ch is None or gross <= 0:
        return []
    issues: list[dict] = []
    ac = inv.get("allowances_charges") or []

    # ── Discounts ──────────────────────────────────────────────────────────
    sent: dict[str, list[float]] = {}
    for a in ac:
        if a.get("type") == "Allowance":
            sent.setdefault(str(a.get("code")), []).append(round(float(a.get("amount") or 0), 2))
    trade, ibx = round(gross * 0.005, 2), round(gross * 0.0125, 2)
    base = {"gross": gross, "subtotal": gross - trade - ibx}
    wanted = {code for code, *_ in DISCOUNTS[ch]}
    for code, name, rate, basis in DISCOUNTS[ch]:
        exp = round(base[basis] * rate, 2)
        got = sent.get(code) or []
        if not got:
            issues.append(_issue(f"discount_missing:{code}", f"{name} discount not on the invoice (about {_money(exp)})", PARK))
            continue
        if len(got) > 1:
            issues.append(_issue(f"discount_twice:{code}", f"{name} discount appears {len(got)} times ({_money(sum(got))})", PARK))
            continue
        diff = round(got[0] - exp, 2)
        if abs(diff) > max(0.05, 0.01 * exp):
            why = " (calculated on gross rather than the subtotal)" if code == "E210" and abs(got[0] - round(gross * rate, 2)) <= 0.02 else ""
            issues.append(_issue(f"discount_off:{code}",
                                 f"{name} discount {_money(got[0])} vs {_money(exp)} expected{why}",
                                 UNDERPAID if diff > 0 else PARK))
    for code, amts in sent.items():
        if code not in wanted:
            issues.append(_issue(f"discount_extra:{code}", f"Extra discount code {code} ({_money(sum(amts))})", UNDERPAID))

    # ── Tax (HD's VAT matrix; no PST anywhere) ─────────────────────────────
    net = round(gross - float(inv.get("allowance_amount") or 0) - float(inv.get("discount_amount") or 0), 2)
    if ch == "dsd":
        taxes = {str(a.get("code")): float(a.get("amount") or 0) for a in ac
                 if a.get("category") == "tax" and float(a.get("amount") or 0)}
    else:
        taxes = {}
        for t in inv.get("txi") or []:
            if float(t.get("amount") or 0):
                taxes[str(t.get("code"))] = taxes.get(str(t.get("code")), 0) + float(t["amount"])
    province = str(inv.get("province") or "").upper()
    exp_tax = expected_tax(province, ch)
    pst_code = "H680" if ch == "dsd" else "ST"
    tax_total = round(sum(taxes.values()), 2)
    if exp_tax and net > 0:
        code_problem = False
        if province != "QC" and pst_code in taxes:
            code_problem = True
            issues.append(_issue("tax_pst", f"Includes provincial sales tax {_money(taxes[pst_code])} -- HD is PST-exempt, "
                                            f"so {province} is {exp_tax['label']} only", REJECT))
        wrong = sorted(set(taxes) - exp_tax["codes"] - {pst_code})
        if wrong:
            code_problem = True
            issues.append(_issue("tax_code", f"Tax sent under code {', '.join(wrong)}; HD expects "
                                             f"{' + '.join(sorted(exp_tax['codes']))} for {province} ({exp_tax['label']})", TAX_RISK))
        if not taxes:
            issues.append(_issue("tax_none", f"No tax on the invoice ({province} is {exp_tax['label']}, about "
                                             f"{_money(net * exp_tax['rate'])})", NO_TAX))
        elif not code_problem:
            if province == "QC" and len(set(taxes) & exp_tax["codes"]) < 2:
                issues.append(_issue("tax_qst_missing", f"Quebec invoice without both GST and QST ({_money(tax_total)} tax)", REJECT))
            else:
                exp_amt = round(net * exp_tax["rate"], 2)
                if abs(tax_total - exp_amt) > max(0.03, 0.0002 * net):
                    issues.append(_issue("tax_rate", f"Tax {_money(tax_total)} ({100 * tax_total / net:.2f}% of net); "
                                                     f"{province} is {exp_tax['label']} (about {_money(exp_amt)})", REJECT))

    # ── Header ─────────────────────────────────────────────────────────────
    vendor = str(inv.get("vendor_number") or "")
    if not vendor:
        issues.append(_issue("vendor_missing", "No pay-to vendor number on the invoice", REJECT))
    elif not vendor.startswith(("25", "26", "27")):
        issues.append(_issue("vendor_number", f"Vendor number {vendor} differs from our pay-to number", REJECT))
    number = str(inv.get("invoice_number") or "")
    if not re.fullmatch(r"[A-Za-z0-9]{1,16}", number):
        issues.append(_issue("invoice_number", f"Invoice number '{number}' -- HD allows up to 16 letters/digits", REJECT))
    if ch == "dsd" and not inv.get("gst_registration"):
        issues.append(_issue("gst_number", "GST/HST registration number not on the invoice", TAX_RISK))
    total = round(float(inv.get("total_amount") or 0), 2)
    computed = round(net + float(inv.get("freight_amount") or 0) + float(inv.get("fee_amount") or 0)
                     + float(inv.get("tax_amount") or 0) + (tax_total if ch == "dropship" else 0), 2)
    if abs(total - computed) > 0.01:
        issues.append(_issue("total_off", f"Total {_money(total)} differs from lines, discounts and tax ({_money(computed)})", REJECT))
    return issues


BOOKS = "Our books to correct"


def changed_after_push(inv: dict, snaps: dict, netsuite_total: float | None = None) -> list[dict]:
    """HD was billed a different amount from what the invoice now says. CRSTL edits an
    ACCEPTED invoice in place, keeping the transaction id (six SK invoices lost their PST
    that way on 2026-09-22) -- but HD keeps what it accepted, and an accepted invoice can
    never be re-sent (HD CMP payables doc p.8, p.9 E336, p.15). HD settles the difference
    with a chargeback (812; taxes are a chargeback type, p.18-19).

    Worded for ACCOUNTING, who see NetSuite and HD's portal, never CRSTL (Ritchie,
    2026-09-25): no drafts or versions, and the action is about the chargeback, not an
    edit. `snaps` is {target: {"hd_total": float}} -- the 810's value when we pushed it,
    i.e. what HD was billed. `netsuite_total` is NetSuite's CURRENT total for the entry
    (None = not read / not found): when it no longer matches what HD was billed, NetSuite
    already reflects the correction (INV538740348) and the chargeback must not be applied
    to it a second time."""
    now = round(float(inv.get("total_amount") or 0), 2)
    # round the difference itself: 107.15 - 107.14 is a hair over 0.01 in binary floats,
    # and a cent of rounding is not a change worth reporting.
    stale = {t: s for t, s in (snaps or {}).items()
             if s.get("hd_total") is not None and round(abs(round(float(s["hd_total"]), 2) - now), 2) > 0.01}
    if not stale:
        return []
    billed = round(float((stale.get("netsuite") or next(iter(stale.values())))["hd_total"]), 2)
    diff = round(billed - now, 2)
    if diff < 0:
        return [_issue("changed_after_push",
                       f"HD was billed {_money(billed)}; the correct amount is {_money(now)} ({_money(-diff)} more)",
                       "HD won't pay the difference unless it is claimed in HD's portal")]
    problem = f"HD was billed {_money(billed)}; the correct amount is {_money(now)}"
    if netsuite_total is not None and abs(round(float(netsuite_total), 2) - billed) > 0.05:
        return [_issue("changed_after_push", f"{problem}. NetSuite already has {_money(netsuite_total)}",
                       f"Expect an HD chargeback of about {_money(diff)}. NetSuite already reflects it: "
                       "don't reduce this entry again")]
    checked = "" if netsuite_total is not None else " (NetSuite not checked today)"
    return [_issue("changed_after_push", f"{problem}. Accepted invoices can't be re-sent",
                   f"Expect an HD chargeback of about {_money(diff)}. It is valid: match it to this invoice{checked}")]


def _created(inv: dict) -> str:
    return str(inv.get("created_at") or "")


def hd_reject_issues(rejects: list[dict], po_number: str, accepted_by_po: dict) -> list[dict]:
    """HD's 864 rejections for one invoice, as issues -- unless the PO has since been
    billed again and accepted, which is the only thing that settles a rejection.

    `rejects` = [{"id", "at", "errors"}] (app.edi_rejects); `accepted_by_po` =
    {po: [created_at, ...]} over every accepted invoice, so a replacement created after
    the rejection clears it. Rejections of an invoice we never re-billed stay listed."""
    out = []
    for rej in sorted(rejects, key=lambda r: str(r.get("at") or "")):
        at = str(rej.get("at") or "")
        if any(c > at for c in accepted_by_po.get(str(po_number), [])):
            continue
        when = _et_date(at)
        errors = "; ".join(str(e) for e in (rej.get("errors") or [])) or "no reason given"
        out.append(_issue(f"hd_reject:{rej.get('id')}", f"HD returned it on {when}: {errors}", RETURNED))
    return out


def _et_date(iso: str) -> str:
    from zoneinfo import ZoneInfo
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("America/Toronto")).strftime("%d %b")
    except (TypeError, ValueError):
        return "an unknown date"


def check_all(invoices: list[dict], created_after: str, rejections: dict | None = None) -> dict:
    """Check what HD has: the latest ACCEPTED version of each invoice number created on
    or after `created_after` (YYYY-MM-DD). A Draft of the same number created later is
    reported: a correction exists but was never sent (HD still has the old one).

    `rejections` = {invoice number: [{"id", "at", "errors"}]}, HD's 864s, already floored
    at the go-live date by the caller. Those are reported whenever HD sent one, even for
    an invoice older than `created_after`: HD's verdict is news whenever it arrives.

    Returns {"checked": n, "latest": [invoice, ...], "results": {invoice_number:
             {"invoice", "issues", "draft_at"}}, "dropship_without_gst": n}."""
    latest: dict[str, dict] = {}
    drafts: dict[str, str] = {}
    accepted: dict[str, dict] = {}          # every accepted invoice, no date floor
    accepted_by_po: dict[str, list[str]] = {}
    for inv in invoices:
        num = str(inv.get("invoice_number") or "")
        if not num or channel_of(inv) is None:
            continue
        if inv.get("status") == "Draft":
            drafts[num] = max(drafts.get(num, ""), _created(inv))
            continue
        if inv.get("status") != "Accepted":
            continue
        accepted_by_po.setdefault(str(inv.get("po_number") or ""), []).append(_created(inv))
        if num not in accepted or _created(inv) > _created(accepted[num]):
            accepted[num] = inv
        if _created(inv)[:10] < created_after:
            continue
        if num not in latest or _created(inv) > _created(latest[num]):
            latest[num] = inv
    results = {}
    no_gst = 0
    for num, inv in latest.items():
        if channel_of(inv) == "dropship" and not inv.get("gst_registration"):
            no_gst += 1
        issues = check_invoice(inv)
        draft_at = drafts.get(num) if drafts.get(num, "") > _created(inv) else None
        if issues:
            results[num] = {"invoice": inv, "issues": issues, "draft_at": draft_at}
    for num, rejects in (rejections or {}).items():
        inv = accepted.get(num)
        if inv is None:      # HD named an invoice CRSTL has no accepted 810 for
            continue
        issues = hd_reject_issues(rejects, str(inv.get("po_number") or ""), accepted_by_po)
        if not issues:
            continue
        entry = results.setdefault(num, {"invoice": inv, "issues": [],
                                         "draft_at": drafts.get(num) if drafts.get(num, "") > _created(inv) else None})
        entry["issues"] = issues + entry["issues"]      # HD's verdict reads before our guesses
    return {"checked": len(latest), "latest": list(latest.values()), "results": results,
            "dropship_without_gst": no_gst}
