# Reported Tax (`tax_information`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read HD's reported tax from `summary.tax_information`, fold it into the invoice totals, and add advisory checks that surface where numbers differ.

**Architecture:** `app/crstl.py` parses the new array verbatim into `reported_taxes` and folds the total into `tax_amount` / `tax_breakdown` / `discrepancy`, plus a factual `tax_channel` field saying where tax came from. `app/main.py` gains three non-mutating annotators that run after the cache refresh: channel-vs-flavor anomalies, a tax-rate cross-check, and discount-not-applied detection. Downstream (`export.py`, `index.html`) surfaces the new fields.

**Tech Stack:** Python 3.12, FastAPI, pytest, Alpine.js + Tailwind (CDN, no build step).

## Global Constraints

- **Crstl is the system of record.** Annotators must never mutate `tax_amount`, `tax_breakdown`, `discrepancy`, or any raw Crstl value. They only add new keys.
- **Never translate TXI codes.** `CG`/`VA`/`ST` are reported raw. They are absent from the HD 810 spec; any rate-to-meaning reading lives in an advisory annotation only.
- **Tax base is net of allowance and discount:** `subtotal − allowance_amount − discount_amount + freight_amount + fee_amount`. Verified against 42/42 live invoices.
- **Tolerance convention:** `max(0.02, round(0.002 * base, 2))` — matches the existing rule at `app/main.py:182`.
- All money values round to 2 decimals via `round(x, 2)`.
- Run the full suite with `python3 -m pytest tests -q` (the `python` alias does not exist on this machine).

---

### Task 1: Parse `tax_information` verbatim

**Files:**
- Modify: `app/crstl.py:154-194` (the summary-parsing block)
- Test: `tests/test_crstl.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `reported_taxes: list[dict]` on each invoice, entries shaped `{"code": str | None, "amount": float}`. Task 2 consumes this.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_crstl.py`:

```python
def _dropship_detail(tax_information, sac_loop=None, total=44.58, subtotal_qty=43):
    """810 detail shaped like a live Dropship invoice: one line at $1.00,
    optional C300 discount, optional TXI tax."""
    return {
        "metadata": {
            "id": "6a763d5d9003a916a4b203e2",
            "reference_id": "INV538161225",
            "source_document_reference_id": "538161225",
            "trading_partner_name": "Home Depot Canada",
            "trading_partner_flavor": "Dropship",
            "created_at": "2026-08-09T00:00:00.000Z",
            "state": {"value": "Accepted"},
            "value": total,
        },
        "file": {"generic_json_edi": {
            "heading": {
                "invoice_number": "INV538161225",
                "invoice_date": "2026-08-09",
                "purchase_order_number": "538161225",
            },
            "detail": {"baseline_item_data_invoice_loop": [
                {"baseline_item_data_invoice": {
                    "line_item_number": "10",
                    "quantity_invoiced": subtotal_qty,
                    "unit_price": 1.00,
                    "quantity_unit_code": "EA",
                    "vendors_part_number": "069556581869",
                }},
            ]},
            "summary": {
                "total_monetary_value_summary": {"total_amount": total},
                "service_promotion_allowance_or_charge_information_loop": sac_loop or [],
                "tax_information": tax_information,
            },
        }},
    }


C300_DISCOUNT = [{"service_promotion_allowance_or_charge_information": {
    "allowance_or_charge_indicator": "A",
    "service_promotion_allowance_or_charge_code": "C300",
    "amount": 0.54,
}}]


def _client():
    return CrstlClient(base_url="https://api.crstl.so/v2", api_key="ct_live_test")


def test_reported_taxes_parsed_verbatim():
    detail = _dropship_detail([{"tax_type_code": "CG", "monetary_amount": "2.12"}])
    fields = _client()._extract_invoice_fields(detail)
    assert fields["reported_taxes"] == [{"code": "CG", "amount": 2.12}]


def test_reported_taxes_multi_entry_preserves_order_and_codes():
    """Quebec invoices carry GST and QST as two entries. Both are reported raw."""
    detail = _dropship_detail([
        {"tax_type_code": "CG", "monetary_amount": "0.91"},
        {"tax_type_code": "ST", "monetary_amount": "1.82"},
    ])
    fields = _client()._extract_invoice_fields(detail)
    assert fields["reported_taxes"] == [
        {"code": "CG", "amount": 0.91},
        {"code": "ST", "amount": 1.82},
    ]


def test_reported_taxes_null_code_preserved_not_guessed():
    """INV2013003 arrives with a null tax_type_code. Never infer one."""
    detail = _dropship_detail([{"tax_type_code": None, "monetary_amount": "0"}])
    fields = _client()._extract_invoice_fields(detail)
    assert fields["reported_taxes"] == [{"code": None, "amount": 0.0}]


def test_absent_tax_information_yields_empty_list():
    """DSD invoices carry no TXI at all."""
    detail = _dropship_detail([])
    del detail["file"]["generic_json_edi"]["summary"]["tax_information"]
    fields = _client()._extract_invoice_fields(detail)
    assert fields["reported_taxes"] == []


def test_non_numeric_monetary_amount_does_not_raise():
    detail = _dropship_detail([{"tax_type_code": "CG", "monetary_amount": "n/a"}])
    fields = _client()._extract_invoice_fields(detail)
    assert fields["reported_taxes"] == [{"code": "CG", "amount": 0.0}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_crstl.py -k reported_taxes -v`
Expected: FAIL with `KeyError: 'reported_taxes'`

- [ ] **Step 3: Write minimal implementation**

In `app/crstl.py`, immediately after the SAC loop (after the `allowances_charges.append(...)` block ends, before `if subtotal == 0.0:`), insert:

```python
        # Summary-level TXI tax. HD reports Dropship tax here; DSD reports tax
        # through SAC codes instead — no invoice observed carrying both. The
        # codes (CG/VA/ST) do not appear in the HD Canada 810 specification, so
        # they are reported exactly as sent rather than translated into our
        # GST/HST_QST vocabulary. Any rate-to-meaning reading is an advisory
        # annotation in main.py, never a rewrite of this data.
        reported_taxes = []
        for entry in summary.get("tax_information", []) or []:
            reported_taxes.append({
                "code": entry.get("tax_type_code"),
                "amount": round(self._parse_float(entry.get("monetary_amount")), 2),
            })
```

Then add to the returned dict, immediately after the `"tax_breakdown"` line:

```python
            "reported_taxes":   reported_taxes,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_crstl.py -k reported_taxes -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest tests -q`
Expected: all pass — this task adds a field and changes no existing value.

- [ ] **Step 6: Commit**

```bash
git add app/crstl.py tests/test_crstl.py
git commit -m "feat(tax): parse summary.tax_information verbatim into reported_taxes"
```

---

### Task 2: Fold reported tax into totals, add `tax_channel`

**Files:**
- Modify: `app/crstl.py:186-205` (amount rollups and discrepancy), `app/crstl.py:217-222` (stale flavor comments)
- Test: `tests/test_crstl.py`

**Interfaces:**
- Consumes: `reported_taxes` from Task 1.
- Produces: `tax_amount` now includes reported tax; `tax_breakdown` gains raw TXI codes as keys; new `tax_channel: str` with values `"reported"`, `"sac"`, `"both"`, `"none"`. Tasks 3–7 consume `tax_channel`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_crstl.py`:

```python
def test_reported_tax_counts_toward_tax_amount():
    detail = _dropship_detail([{"tax_type_code": "CG", "monetary_amount": "2.12"}],
                             sac_loop=C300_DISCOUNT)
    fields = _client()._extract_invoice_fields(detail)
    assert fields["tax_amount"] == 2.12


def test_reported_tax_keyed_raw_in_breakdown():
    detail = _dropship_detail([{"tax_type_code": "CG", "monetary_amount": "2.12"}])
    fields = _client()._extract_invoice_fields(detail)
    assert fields["tax_breakdown"] == {"CG": 2.12}


def test_reported_tax_closes_the_discrepancy():
    """43.00 subtotal − 0.54 discount + 2.12 tax = 44.58 total. Before we read
    tax_information this invoice showed a 2.12 residual."""
    detail = _dropship_detail([{"tax_type_code": "CG", "monetary_amount": "2.12"}],
                              sac_loop=C300_DISCOUNT, total=44.58)
    fields = _client()._extract_invoice_fields(detail)
    assert fields["discrepancy"] == 0.0


def test_multi_entry_tax_sums_into_amount():
    detail = _dropship_detail([
        {"tax_type_code": "CG", "monetary_amount": "0.91"},
        {"tax_type_code": "ST", "monetary_amount": "1.82"},
    ])
    fields = _client()._extract_invoice_fields(detail)
    assert fields["tax_amount"] == 2.73
    assert fields["tax_breakdown"] == {"CG": 0.91, "ST": 1.82}


def test_uncoded_entry_counts_in_total_but_not_breakdown():
    """An entry with no code still holds money, but there is nothing to key it
    by — it must not invent a bucket."""
    detail = _dropship_detail([
        {"tax_type_code": None, "monetary_amount": "1.50"},
        {"tax_type_code": "CG", "monetary_amount": "2.00"},
    ])
    fields = _client()._extract_invoice_fields(detail)
    assert fields["tax_amount"] == 3.50
    assert fields["tax_breakdown"] == {"CG": 2.00}


def test_sac_tax_and_reported_tax_sum_rather_than_one_winning():
    """No live invoice carries both today. If HD ever sends both, show the
    total instead of silently picking one."""
    sac_gst = [{"service_promotion_allowance_or_charge_information": {
        "allowance_or_charge_indicator": "C",
        "service_promotion_allowance_or_charge_code": "D360",
        "amount": 1.00,
    }}]
    detail = _dropship_detail([{"tax_type_code": "CG", "monetary_amount": "2.12"}],
                              sac_loop=sac_gst)
    fields = _client()._extract_invoice_fields(detail)
    assert fields["tax_amount"] == 3.12
    assert fields["tax_breakdown"]["CG"] == 2.12
    assert fields["tax_breakdown"]["GST"] == 1.00
    assert fields["tax_channel"] == "both"


def test_tax_channel_reported_sac_and_none():
    reported = _client()._extract_invoice_fields(
        _dropship_detail([{"tax_type_code": "CG", "monetary_amount": "2.12"}]))
    assert reported["tax_channel"] == "reported"

    sac_gst = [{"service_promotion_allowance_or_charge_information": {
        "allowance_or_charge_indicator": "C",
        "service_promotion_allowance_or_charge_code": "D360",
        "amount": 1.00,
    }}]
    sac = _client()._extract_invoice_fields(_dropship_detail([], sac_loop=sac_gst))
    assert sac["tax_channel"] == "sac"

    none = _client()._extract_invoice_fields(_dropship_detail([]))
    assert none["tax_channel"] == "none"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_crstl.py -k "tax_channel or reported_tax or uncoded or multi_entry_tax" -v`
Expected: FAIL — `tax_amount` is 0.0 and `tax_channel` raises `KeyError`

- [ ] **Step 3: Write minimal implementation**

In `app/crstl.py`, extend the block added in Task 1 with the breakdown merge and total:

```python
        reported_tax_total = 0.0
        for entry in reported_taxes:
            reported_tax_total += entry["amount"]
            # Entries without a code still hold money and count toward the
            # total, but there is nothing to key them by — leave them out of
            # the breakdown rather than invent a bucket. reported_taxes keeps
            # the raw entry either way.
            if entry["code"]:
                key = entry["code"]
                tax_breakdown[key] = round(tax_breakdown.get(key, 0.0) + entry["amount"], 2)
```

Replace the `tax_amount` assignment at `app/crstl.py:194`:

```python
        # Tax is what HD reports, through either channel: SAC tax codes
        # (D360/H680/H850 — how DSD invoices carry it) or summary TXI
        # (how Dropship invoices carry it). We still do NOT derive tax from
        # total − net_taxable; an invoice whose reported tax fails to explain
        # its total must keep a non-zero discrepancy, because surfacing that
        # is the whole point of this dashboard.
        sac_tax = round(category_totals["tax"], 2)
        tax_amount = round(sac_tax + reported_tax_total, 2)

        if sac_tax and reported_tax_total:
            tax_channel = "both"
        elif reported_tax_total:
            tax_channel = "reported"
        elif sac_tax:
            tax_channel = "sac"
        else:
            tax_channel = "none"
```

Add to the returned dict, immediately after `"reported_taxes"`:

```python
            "tax_channel":      tax_channel,
```

Replace the stale flavor comment at `app/crstl.py:217-222` with:

```python
            # Flavor distinguishes the two invoicing models, which differ in how
            # tax arrives: "Dropship" (shipped direct to consumer) reports tax
            # in summary.tax_information and never carries freight/fee;
            # "Direct Store Delivery (DSD)" reports tax through SAC codes and
            # always carries an allowance or discount. "Wholesale" (delivered to
            # the VAUGHAN/CALGARY DC) shows neither in the sample to date.
            # Downstream annotation uses this to know what "normal" looks like.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_crstl.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest tests -q`
Expected: all pass. If a NetSuite or export test asserts a zero tax on a Dropship fixture, update that fixture's expectation — reported tax now flows through, which is the intended change.

- [ ] **Step 6: Commit**

```bash
git add app/crstl.py tests/test_crstl.py
git commit -m "feat(tax): count reported tax toward tax_amount, breakdown and discrepancy"
```

---

### Task 3: Flag channel-vs-flavor anomalies

**Files:**
- Modify: `app/main.py` (add annotator after `_annotate_tax_suggestion`, call it in `_refresh_cache`)
- Test: `tests/test_tax_checks.py` (create)

**Interfaces:**
- Consumes: `tax_channel`, `trading_partner_flavor`, `freight_amount`, `fee_amount`.
- Produces: `_annotate_channel_anomalies(invoices: list[dict]) -> None`, adding `channel_anomalies: list[str]` when something departs from the flavor's norm. Tasks 6 and 7 render it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tax_checks.py`:

```python
"""Advisory checks over reported tax. Every annotator here is additive — it
must never alter a value Crstl sent, because Crstl is what HD receives."""

from app.main import _annotate_channel_anomalies


def _inv(**overrides):
    inv = {
        "trading_partner_flavor": "Dropship",
        "tax_channel": "reported",
        "freight_amount": 0.0,
        "fee_amount": 0.0,
        "tax_amount": 2.12,
    }
    inv.update(overrides)
    return inv


def test_dropship_reporting_via_txi_is_normal():
    inv = _inv()
    _annotate_channel_anomalies([inv])
    assert "channel_anomalies" not in inv


def test_dsd_reporting_via_sac_is_normal():
    inv = _inv(trading_partner_flavor="Direct Store Delivery (DSD)", tax_channel="sac")
    _annotate_channel_anomalies([inv])
    assert "channel_anomalies" not in inv


def test_dsd_carrying_txi_is_flagged():
    inv = _inv(trading_partner_flavor="Direct Store Delivery (DSD)", tax_channel="reported")
    _annotate_channel_anomalies([inv])
    assert len(inv["channel_anomalies"]) == 1
    assert "reported" in inv["channel_anomalies"][0]


def test_dropship_carrying_sac_tax_is_flagged():
    inv = _inv(tax_channel="sac")
    _annotate_channel_anomalies([inv])
    assert len(inv["channel_anomalies"]) == 1


def test_dropship_with_freight_is_flagged():
    """No Dropship invoice in the sample carries freight or fee."""
    inv = _inv(freight_amount=5.00)
    _annotate_channel_anomalies([inv])
    assert any("freight" in f for f in inv["channel_anomalies"])


def test_no_tax_at_all_is_not_an_anomaly():
    """17 Dropship invoices report no tax and reconcile cleanly."""
    inv = _inv(tax_channel="none", tax_amount=0.0)
    _annotate_channel_anomalies([inv])
    assert "channel_anomalies" not in inv


def test_wholesale_has_no_established_norm():
    """n=2 in the sample — record the channel, assert nothing."""
    inv = _inv(trading_partner_flavor="Wholesale", tax_channel="sac")
    _annotate_channel_anomalies([inv])
    assert "channel_anomalies" not in inv


def test_annotator_never_mutates_raw_values():
    inv = _inv(tax_channel="sac", tax_amount=2.12)
    _annotate_channel_anomalies([inv])
    assert inv["tax_amount"] == 2.12
    assert inv["tax_channel"] == "sac"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_tax_checks.py -v`
Expected: FAIL with `ImportError: cannot import name '_annotate_channel_anomalies'`

- [ ] **Step 3: Write minimal implementation**

In `app/main.py`, after `_annotate_tax_suggestion`, add:

```python
# What "normal" looks like per invoicing model, from a survey of all 113 live
# invoices on 2026-08-10: Dropship carries tax in summary.tax_information and
# never carries freight/fee; DSD carries tax through SAC codes and never
# carries TXI. Wholesale (n=2) showed neither, which is too small a sample to
# assert anything from.
_EXPECTED_TAX_CHANNEL = {
    "Dropship": "reported",
    "Direct Store Delivery (DSD)": "sac",
}


def _annotate_channel_anomalies(invoices: list[dict]) -> None:
    """Flag invoices whose tax arrived through the wrong channel for their
    flavor, or that carry charges their flavor never carries. Purely additive —
    a departure from the norm is a lead to chase, not a value to correct."""
    for inv in invoices:
        flavor = inv.get("trading_partner_flavor") or ""
        expected = _EXPECTED_TAX_CHANNEL.get(flavor)
        if not expected:
            continue

        flags = []
        channel = inv.get("tax_channel") or "none"
        # "none" is unremarkable: plenty of invoices genuinely carry no tax.
        if channel != "none" and channel != expected:
            flags.append(
                f"tax arrived via {channel}; {flavor} normally reports via {expected}"
            )
        if flavor == "Dropship" and ((inv.get("freight_amount") or 0.0)
                                     or (inv.get("fee_amount") or 0.0)):
            flags.append("freight/fee present; not observed on Dropship invoices")

        if flags:
            inv["channel_anomalies"] = flags
```

In `_refresh_cache`, add the call after `_annotate_tax_suggestion(invoices)`:

```python
        _annotate_channel_anomalies(invoices)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_tax_checks.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_tax_checks.py
git commit -m "feat(tax): flag tax channel that departs from the flavor's norm"
```

---

### Task 4: Repoint the suggestion into a rate cross-check

**Files:**
- Modify: `app/main.py:138-189` (`_PROVINCE_TAX_RATES` comment and `_annotate_tax_suggestion`)
- Test: `tests/test_tax_suggest.py`

**Interfaces:**
- Consumes: `tax_amount` (post-Task 2), `province`, and the net-base components.
- Produces: `tax_rate_check: dict` with keys `reported`, `implied_rate`, `expected_kind`, `expected_rate`, `expected_amount`, `province` — set only when reported tax diverges from the province rate. The existing `tax_suggestion` key keeps its current shape and now only appears on invoices reporting no tax.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_tax_suggest.py`:

```python
def test_reported_tax_matching_province_rate_is_silent():
    """13% of 19.75 = 2.57. HD's number agrees with the rate — say nothing."""
    inv = _base(province="ON", subtotal=20.0, discount_amount=0.25,
                tax_amount=2.57, discrepancy=0.0, total_amount=22.32)
    _annotate_tax_suggestion([inv])
    assert "tax_rate_check" not in inv
    assert "tax_suggestion" not in inv


def test_reported_tax_at_wrong_rate_is_flagged():
    """GST 5% booked on an Ontario invoice that should carry HST 13% — the
    mis-mapping this check exists to catch."""
    inv = _base(province="ON", subtotal=20.0, discount_amount=0.25,
                tax_amount=0.99, discrepancy=0.0, total_amount=20.74)
    _annotate_tax_suggestion([inv])
    check = inv["tax_rate_check"]
    assert check["reported"] == 0.99
    assert check["expected_kind"] == "HST"
    assert check["expected_rate"] == 0.13
    assert check["expected_amount"] == 2.57
    assert check["province"] == "ON"


def test_rate_check_uses_net_base_not_gross():
    """5% of the 19.75 net = 0.99; 5% of the 20.00 gross = 1.00. The net base
    reproduced reported tax on 42/42 live invoices, so 0.99 must pass clean."""
    inv = _base(province="BC", subtotal=20.0, discount_amount=0.25,
                tax_amount=0.99, discrepancy=0.0, total_amount=20.74)
    _annotate_tax_suggestion([inv])
    assert "tax_rate_check" not in inv


def test_rate_check_never_mutates_reported_tax():
    inv = _base(province="ON", subtotal=20.0, discount_amount=0.25,
                tax_amount=0.99, discrepancy=0.0, total_amount=20.74)
    _annotate_tax_suggestion([inv])
    assert inv["tax_amount"] == 0.99
    assert inv["discrepancy"] == 0.0


def test_no_province_means_no_rate_check():
    inv = _base(province=None, tax_amount=0.99, discrepancy=0.0)
    _annotate_tax_suggestion([inv])
    assert "tax_rate_check" not in inv


def test_suggestion_still_fires_when_no_tax_reported():
    """The 17 Dropship invoices Crstl reports no tax for keep the old behaviour."""
    inv = _base()  # tax_amount 0.0, discrepancy 2.57, BC
    _annotate_tax_suggestion([inv])
    assert inv["tax_suggestion"]["amount"] == 2.57
    assert "tax_rate_check" not in inv
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_tax_suggest.py -k "rate_check or reported_tax or suggestion_still" -v`
Expected: FAIL — `tax_rate_check` is never set, and `test_reported_tax_matching_province_rate_is_silent` fails because the old code ignores `tax_amount`

- [ ] **Step 3: Write minimal implementation**

Replace the comment at `app/main.py:138-143` with:

```python
# Standard Canadian sales-tax rates per ship-to province. Used ONLY for
# advisory annotation: cross-checking the tax HD reports against the rate its
# province implies, and — where HD reports no tax at all — hinting that an
# unexplained residual matches the province rate. Never a substitute for HD's
# own figure, which arrives via summary.tax_information or SAC tax codes.
```

Replace the body of `_annotate_tax_suggestion` with:

```python
def _annotate_tax_suggestion(invoices: list[dict]) -> None:
    """Two advisory annotations, both non-mutating.

    Where HD reports tax, cross-check it against the ship-to province's
    standard rate and set `tax_rate_check` when they diverge — that is a
    mis-mapped tax code, and the reported value still stands.

    Where HD reports no tax, keep the original behaviour: if the unexplained
    residual matches the province rate, set `tax_suggestion` so accounting can
    tell "expected tax per rate" from "real HD data error worth chasing"."""
    for inv in invoices:
        province = inv.get("province")
        rate_info = _PROVINCE_TAX_RATES.get(province) if province else None
        net_taxable = (
            (inv.get("subtotal") or 0.0)
            - (inv.get("allowance_amount") or 0.0)
            - (inv.get("discount_amount") or 0.0)
            + (inv.get("freight_amount") or 0.0)
            + (inv.get("fee_amount") or 0.0)
        )
        if not rate_info or net_taxable <= 0:
            continue
        kind, rate = rate_info
        expected = round(net_taxable * rate, 2)
        # Tolerance: 2 cents floor, 0.2% of net for larger invoices. Handles
        # cent-rounding on individual line items without matching random noise.
        tolerance = max(0.02, round(0.002 * net_taxable, 2))

        reported = round(inv.get("tax_amount") or 0.0, 2)
        if reported:
            if abs(expected - reported) > tolerance:
                inv["tax_rate_check"] = {
                    "reported": reported,
                    "implied_rate": round(reported / net_taxable, 5),
                    "expected_kind": kind,
                    "expected_rate": rate,
                    "expected_amount": expected,
                    "province": province,
                }
            continue

        residual = round(inv.get("discrepancy") or 0.0, 2)
        if residual <= 0.01:  # only positive residuals could be missing tax
            continue
        if abs(expected - residual) <= tolerance:
            inv["tax_suggestion"] = {
                "kind": kind,
                "rate": rate,
                "amount": expected,
                "province": province,
            }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_tax_suggest.py -v`
Expected: PASS — all pre-existing suggestion tests still pass, because every one of them uses `tax_amount: 0.0`

- [ ] **Step 5: Update the module docstring**

Replace the docstring at the top of `tests/test_tax_suggest.py`:

```python
"""Coverage for `_annotate_tax_suggestion`, which does two advisory jobs:
cross-checks the tax HD reports against the ship-to province's rate, and — on
invoices where HD reports no tax — hints that an unexplained residual matches
that rate. Both are purely additive; neither may mutate tax_amount,
tax_breakdown or discrepancy, so the "report what Crstl says" contract holds."""
```

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_tax_suggest.py
git commit -m "feat(tax): cross-check reported tax against province rate"
```

---

### Task 5: Detect discounts reported but not applied

**Files:**
- Modify: `app/main.py` (add annotator, call it in `_refresh_cache`)
- Test: `tests/test_tax_checks.py`

**Interfaces:**
- Consumes: `subtotal`, `allowance_amount`, `discount_amount`, `tax_amount`, `total_amount`.
- Produces: `_annotate_discount_not_applied(invoices: list[dict]) -> None`, adding `discount_not_applied: {"amount": float}`. Tasks 6 and 7 render it.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_tax_checks.py`:

```python
from app.main import _annotate_discount_not_applied


def _disc(**overrides):
    """INV537858507 exactly: HD deducts the C300 discount when computing tax,
    then bills as though it never existed."""
    inv = {
        "subtotal": 43.00,
        "allowance_amount": 0.0,
        "discount_amount": 0.54,
        "tax_amount": 2.12,
        "total_amount": 45.12,
    }
    inv.update(overrides)
    return inv


def test_discount_deducted_from_tax_but_not_total_is_flagged():
    inv = _disc()
    _annotate_discount_not_applied([inv])
    assert inv["discount_not_applied"] == {"amount": 0.54}


def test_correctly_discounted_invoice_is_silent():
    """Same invoice billed properly: 43.00 - 0.54 + 2.12 = 44.58."""
    inv = _disc(total_amount=44.58)
    _annotate_discount_not_applied([inv])
    assert "discount_not_applied" not in inv


def test_no_deduction_means_nothing_to_flag():
    inv = _disc(discount_amount=0.0, total_amount=45.12)
    _annotate_discount_not_applied([inv])
    assert "discount_not_applied" not in inv


def test_allowance_counts_the_same_as_discount():
    inv = _disc(discount_amount=0.0, allowance_amount=0.54)
    _annotate_discount_not_applied([inv])
    assert inv["discount_not_applied"] == {"amount": 0.54}


def test_cent_rounding_does_not_defeat_the_match():
    inv = _disc(total_amount=45.13)
    _annotate_discount_not_applied([inv])
    assert inv["discount_not_applied"] == {"amount": 0.54}


def test_annotator_never_mutates_totals():
    inv = _disc()
    _annotate_discount_not_applied([inv])
    assert inv["total_amount"] == 45.12
    assert inv["discount_amount"] == 0.54
    assert inv["tax_amount"] == 2.12
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_tax_checks.py -k discount -v`
Expected: FAIL with `ImportError: cannot import name '_annotate_discount_not_applied'`

- [ ] **Step 3: Write minimal implementation**

In `app/main.py`, after `_annotate_channel_anomalies`, add:

```python
def _annotate_discount_not_applied(invoices: list[dict]) -> None:
    """Flag invoices whose total ignores a deduction that HD nonetheless
    honoured when computing tax.

    Seen on four live invoices, all carrying SAC C300 "Discount". INV537858507:
    subtotal 43.00, discount 0.54, tax 2.12, total 45.12. The tax is 5% of
    42.46 — net of the discount — but the total is 43.00 + 2.12, as if the
    discount never happened. Advisory only: the reported total still stands,
    because it is what HD receives."""
    for inv in invoices:
        deduction = round((inv.get("allowance_amount") or 0.0)
                          + (inv.get("discount_amount") or 0.0), 2)
        if deduction <= 0.01:
            continue
        subtotal = inv.get("subtotal") or 0.0
        undeducted_total = subtotal + (inv.get("tax_amount") or 0.0)
        tolerance = max(0.02, round(0.002 * subtotal, 2))
        if abs((inv.get("total_amount") or 0.0) - undeducted_total) <= tolerance:
            inv["discount_not_applied"] = {"amount": deduction}
```

In `_refresh_cache`, add the call after `_annotate_channel_anomalies(invoices)`:

```python
        _annotate_discount_not_applied(invoices)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_tax_checks.py -v`
Expected: PASS (14 tests)

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest tests -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_tax_checks.py
git commit -m "feat(tax): detect discounts deducted from tax but not from the total"
```

---

### Task 6: Surface the new fields in the CSV export

**Files:**
- Modify: `app/export.py:24-48` (`TAIL_COLUMNS`), `app/export.py:80-95` (row build)
- Test: `tests/test_export.py`

**Interfaces:**
- Consumes: `tax_channel`, `reported_taxes`, `tax_rate_check`, `discount_not_applied`.
- Produces: four new CSV columns — `Tax Channel`, `Reported Tax Codes`, `Rate Check`, `Discount Not Applied`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_export.py`:

```python
def test_reported_tax_columns_present():
    from app.export import build_csv
    invoices = [{
        "transaction_id": "tx-1", "invoice_number": "INV538161225",
        "subtotal": 43.00, "tax_amount": 2.12, "total_amount": 44.58,
        "discrepancy": 0.0, "tax_channel": "reported",
        "tax_breakdown": {"CG": 2.12},
        "reported_taxes": [{"code": "CG", "amount": 2.12}],
    }]
    text = build_csv(invoices).decode("utf-8")
    header = text.splitlines()[0]
    assert "Tax Channel" in header
    assert "Reported Tax Codes" in header
    assert "reported" in text
    assert "CG 2.12" in text


def test_rate_check_and_discount_columns_render():
    from app.export import build_csv
    invoices = [{
        "transaction_id": "tx-2", "invoice_number": "INV537858507",
        "subtotal": 43.00, "tax_amount": 2.12, "total_amount": 45.12,
        "discrepancy": 0.54, "tax_channel": "reported",
        "tax_breakdown": {"CG": 2.12},
        "reported_taxes": [{"code": "CG", "amount": 2.12}],
        "tax_rate_check": {
            "reported": 2.12, "implied_rate": 0.05, "expected_kind": "HST",
            "expected_rate": 0.13, "expected_amount": 5.52, "province": "ON",
        },
        "discount_not_applied": {"amount": 0.54},
    }]
    text = build_csv(invoices).decode("utf-8")
    assert "expected HST 13%" in text
    assert "0.54" in text


def test_clean_invoice_leaves_check_columns_blank():
    from app.export import build_csv
    invoices = [{
        "transaction_id": "tx-3", "invoice_number": "INV1",
        "subtotal": 10.0, "tax_amount": 0.0, "total_amount": 10.0,
        "discrepancy": 0.0, "tax_channel": "none",
        "tax_breakdown": {}, "reported_taxes": [],
    }]
    row = build_csv(invoices).decode("utf-8").splitlines()[1]
    assert ",," in row  # adjacent blank check columns
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_export.py -k "reported_tax or rate_check or check_columns" -v`
Expected: FAIL with `KeyError: 'Tax Channel'` or a missing-substring assertion

- [ ] **Step 3: Write minimal implementation**

In `app/export.py`, replace the suggested-tax comment block and columns inside `TAIL_COLUMNS` with:

```python
    # Suggested-tax columns: populated only when HD reports NO tax and a
    # non-zero Discrepancy matches the ship-to province's standard rate.
    # NOT from Crstl — computed locally as a cross-check hint.
    ("Suggested Tax Type",   "_suggested_tax_kind"),
    ("Suggested Tax Rate",   "_suggested_tax_rate"),
    ("Suggested Tax Amount", "_suggested_tax_amount"),
    # Where tax came from: "reported" (summary.tax_information, how Dropship
    # carries it), "sac" (SAC tax codes, how DSD carries it), "both", or "none".
    ("Tax Channel",          "tax_channel"),
    # HD's raw TXI codes and amounts, exactly as sent. These codes are absent
    # from the HD 810 spec, so they are never translated into the Tax GST /
    # Tax HST/QST columns above.
    ("Reported Tax Codes",   "_reported_tax_codes"),
    # Populated when reported tax disagrees with the province's rate — a
    # mis-mapped tax code. The reported value still stands.
    ("Rate Check",           "_rate_check"),
    # Populated when the total ignores a discount that HD honoured when
    # computing tax. Filter this column to find billing errors.
    ("Discount Not Applied", "_discount_not_applied"),
```

In the row build, after the existing suggested-tax lines, add:

```python
        row["Reported Tax Codes"] = " ".join(
            f"{e['code'] or '?'} {e['amount']}" for e in (inv.get("reported_taxes") or [])
        )
        rc = inv.get("tax_rate_check")
        row["Rate Check"] = (
            f"{rc['reported']} at {rc['implied_rate'] * 100:.2f}% — "
            f"expected {rc['expected_kind']} {rc['expected_rate'] * 100:g}% "
            f"= {rc['expected_amount']} ({rc['province']})"
        ) if rc else ""
        dna = inv.get("discount_not_applied")
        row["Discount Not Applied"] = dna["amount"] if dna else ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_export.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest tests -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add app/export.py tests/test_export.py
git commit -m "feat(tax): add reported-tax and cross-check columns to the CSV export"
```

---

### Task 7: Show reported tax and its checks in the dashboard

**Files:**
- Modify: `app/static/index.html:325-385` (the Tax and Discrepancy blocks)
- Test: manual — no JS test harness exists in this repo

**Interfaces:**
- Consumes: `tax_channel`, `reported_taxes`, `tax_rate_check`, `discount_not_applied`, `channel_anomalies`.
- Produces: no new interface; terminal task.

- [ ] **Step 1: Show the tax source next to the amount**

The existing branch at `index.html:331` shows a suggestion *instead of* tax whenever `tax_amount` is 0. That branch now inverts for 43 Dropship invoices, which is correct and needs no change. Add a source line directly beneath the tax `<dd>` at `index.html:332`:

```html
                  <dd class="text-xs text-gray-400 mt-0.5"
                      x-show="activeInvoice.tax_channel === 'reported'"
                      x-text="'Reported by HD: ' + (activeInvoice.reported_taxes || [])
                                .map(t => (t.code || '?') + ' ' + fmtMoney(t.amount)).join(', ')"></dd>
```

- [ ] **Step 2: Add the rate-check warning**

After the suggestion block that ends at `index.html:361`, add:

```html
              <template x-if="activeInvoice.tax_rate_check">
                <div class="mt-2 rounded border border-amber-200 bg-amber-50 px-2 py-1.5 text-xs">
                  <div class="font-medium text-amber-800">Tax rate looks wrong</div>
                  <div class="text-amber-700 mt-0.5">
                    HD reported <span class="font-mono"
                      x-text="fmtMoney(activeInvoice.tax_rate_check.reported)"></span>
                    (<span x-text="(activeInvoice.tax_rate_check.implied_rate * 100).toFixed(2) + '%'"></span>),
                    but <span x-text="activeInvoice.tax_rate_check.province"></span>
                    implies <span x-text="activeInvoice.tax_rate_check.expected_kind"></span>
                    <span x-text="(activeInvoice.tax_rate_check.expected_rate * 100)
                                    .toFixed(3).replace(/\.?0+$/, '') + '%'"></span>
                    = <span class="font-mono"
                      x-text="fmtMoney(activeInvoice.tax_rate_check.expected_amount)"></span>.
                  </div>
                  <div class="text-amber-600 mt-1">Reported value stands — verify the tax code with HD.</div>
                </div>
              </template>
```

- [ ] **Step 3: Add the discount and channel warnings**

Immediately after the block from Step 2:

```html
              <template x-if="activeInvoice.discount_not_applied">
                <div class="mt-2 rounded border border-red-200 bg-red-50 px-2 py-1.5 text-xs">
                  <div class="font-medium text-red-800">Discount not applied to total</div>
                  <div class="text-red-700 mt-0.5">
                    HD deducted <span class="font-mono"
                      x-text="fmtMoney(activeInvoice.discount_not_applied.amount)"></span>
                    when computing tax, but billed as if it never happened.
                  </div>
                </div>
              </template>
              <template x-if="activeInvoice.channel_anomalies">
                <div class="mt-2 rounded border border-amber-200 bg-amber-50 px-2 py-1.5 text-xs text-amber-700">
                  <template x-for="f in activeInvoice.channel_anomalies" :key="f">
                    <div x-text="f"></div>
                  </template>
                </div>
              </template>
```

- [ ] **Step 4: Update the discrepancy hint**

At `index.html:381`, the hint says "Likely missing tax or charge code" whenever the residual is positive and there is no suggestion. Replace that line with one that no longer blames missing tax when tax is present:

```html
                    <span x-show="activeInvoice.discrepancy > 0 && !activeInvoice.tax_suggestion && !activeInvoice.tax_amount">Likely missing tax or charge code.</span>
                    <span x-show="activeInvoice.discrepancy > 0 && activeInvoice.tax_amount && !activeInvoice.discount_not_applied">Tax is reported but does not explain the residual.</span>
```

- [ ] **Step 5: Verify in the running app**

```bash
MOCK_DATA=true SCHEDULER_ENABLED=false python3 -m uvicorn app.main:app --port 8123
```

Open `http://localhost:8123`, open an invoice, and confirm: tax renders with its "Reported by HD" source line; no console errors; a clean invoice shows no warning boxes. Stop the server when done.

- [ ] **Step 6: Commit**

```bash
git add app/static/index.html
git commit -m "ui: show reported tax source and surface rate, discount and channel checks"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §1 Parse verbatim | Task 1 |
| §2 Tax and discrepancy | Task 2 |
| §3 Flavor-aware expectations (`tax_channel`) | Task 2 (field), Task 3 (checks) |
| §4 Rate cross-check | Task 4 |
| §5 Discount not applied | Task 5 |
| §6 Remaining unreconciled invoices — no special handling | No task needed; they simply keep a non-zero discrepancy |
| Stale comments at `crstl.py:217-222` | Task 2, Step 3 |
| Stale comment at `main.py:138-143` | Task 4, Step 3 |
| Blast radius: `export.py` | Task 6 |
| Blast radius: `index.html` | Task 7 |
| Blast radius: `netsuite.py`, `netsuite_csv.py` | No code change — both read `invoice["tax_amount"]`, which Task 2 populates. Task 2 Step 5 covers the fixture updates. |
| Testing section | Tasks 1–6 |

**Type consistency:** `reported_taxes` entries use `{"code", "amount"}` in Tasks 1, 2, 6, 7. `tax_rate_check` uses `reported`, `implied_rate`, `expected_kind`, `expected_rate`, `expected_amount`, `province` in Tasks 4, 6, 7. `discount_not_applied` uses `{"amount"}` in Tasks 5, 6, 7. `tax_channel` values are `"reported"` / `"sac"` / `"both"` / `"none"` in Tasks 2, 3, 6, 7.

**Note on Task 2, Step 5:** `tests/test_netsuite.py` and `tests/test_export.py` may carry Dropship-shaped fixtures asserting a zero tax. Those expectations change because reported tax now flows through — that is the intended behaviour change, not a regression.
