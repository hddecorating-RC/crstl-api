"""Coverage for `_annotate_tax_suggestion` — a cross-check hint for accounting
while Crstl's public API omits TXI segments on dropship 810s. Suggestion is
purely additive; it must never mutate the raw fields (tax_amount, tax_breakdown,
discrepancy) so the "report what Crstl says" contract still holds."""

from app.main import _annotate_tax_suggestion


def _base(**overrides):
    inv = {
        "province": "BC",
        "subtotal": 52.0,
        "allowance_amount": 0.0,
        "discount_amount": 0.65,
        "freight_amount": 0.0,
        "fee_amount": 0.0,
        "tax_amount": 0.0,
        "tax_breakdown": {},
        "discrepancy": 2.57,
        "total_amount": 53.92,
    }
    inv.update(overrides)
    return inv


def test_bc_residual_matches_gst_rate():
    inv = _base()
    _annotate_tax_suggestion([inv])
    ts = inv.get("tax_suggestion")
    assert ts is not None
    assert ts["kind"] == "GST"
    assert ts["rate"] == 0.05
    assert ts["amount"] == 2.57
    assert ts["province"] == "BC"


def test_raw_fields_never_mutated():
    """The whole point: raw Crstl values stay untouched. Only the suggestion is added."""
    inv = _base()
    _annotate_tax_suggestion([inv])
    assert inv["tax_amount"] == 0.0
    assert inv["tax_breakdown"] == {}
    assert inv["discrepancy"] == 2.57


def test_ontario_residual_matches_hst_13():
    # 13% on $50 = 6.50
    inv = _base(province="ON", subtotal=50.0, discount_amount=0.0, discrepancy=6.50, total_amount=56.50)
    _annotate_tax_suggestion([inv])
    ts = inv["tax_suggestion"]
    assert ts["kind"] == "HST" and ts["rate"] == 0.13 and ts["amount"] == 6.50


def test_quebec_residual_matches_combined_rate():
    # 14.975% on $100 = 14.9750
    inv = _base(province="QC", subtotal=100.0, discount_amount=0.0, discrepancy=14.98, total_amount=114.98)
    _annotate_tax_suggestion([inv])
    ts = inv["tax_suggestion"]
    assert ts["kind"] == "GST+QST"
    assert ts["rate"] == 0.14975


def test_residual_matching_no_province_no_suggestion():
    inv = _base(province=None)
    _annotate_tax_suggestion([inv])
    assert "tax_suggestion" not in inv


def test_unmapped_province_no_suggestion():
    inv = _base(province="XX")
    _annotate_tax_suggestion([inv])
    assert "tax_suggestion" not in inv


def test_zero_residual_no_suggestion():
    inv = _base(discrepancy=0.0)
    _annotate_tax_suggestion([inv])
    assert "tax_suggestion" not in inv


def test_negative_residual_no_suggestion():
    """Negative residual can't be a missing tax — it's a real data issue
    (double-counted allowance, voided invoice). Don't paper over it."""
    inv = _base(discrepancy=-5.00, total_amount=46.35)
    _annotate_tax_suggestion([inv])
    assert "tax_suggestion" not in inv


def test_residual_far_from_any_rate_no_suggestion():
    """A residual that doesn't line up with the province rate is a real
    anomaly — leave the discrepancy standing without a false-positive hint."""
    # BC = 5%. Net taxable $51.35, expected GST $2.57. Actual residual $10.00 (nowhere near).
    inv = _base(discrepancy=10.00, total_amount=61.35)
    _annotate_tax_suggestion([inv])
    assert "tax_suggestion" not in inv


def test_zero_net_taxable_no_suggestion():
    """A $0 invoice with residual (a real data issue) can't be tax at any rate."""
    inv = _base(subtotal=0.0, discount_amount=0.0, discrepancy=5.00, total_amount=5.00)
    _annotate_tax_suggestion([inv])
    assert "tax_suggestion" not in inv


def test_tolerance_absorbs_cent_rounding():
    """Real invoices round each line item, so the residual won't exactly equal
    net_taxable * rate. Tolerance must allow small drift."""
    # BC 5% on $51.35 net = $2.5675. Real invoice reports $2.57 (rounded).
    inv = _base(discrepancy=2.57, total_amount=53.92)  # residual is $2.57
    _annotate_tax_suggestion([inv])
    assert inv.get("tax_suggestion") is not None


# ── Accounting's rate sheet, 2026-09-03 ──────────────────────────────────────
# Transcribed from the sheet itself, not from the code it checks. These are the
# rates we charge HD, which is not the same as the rates each province levies:
# BC levies PST at 7% and we do not charge it, so BC is GST-only here.
RATE_SHEET = {
    "AB": ("GST", 0.05),      "BC": ("GST", 0.05),      "MB": ("GST", 0.05),
    "NT": ("GST", 0.05),      "NU": ("GST", 0.05),      "YT": ("GST", 0.05),
    "SK": ("GST+PST", 0.11),  "ON": ("HST", 0.13),      "NS": ("HST", 0.14),
    "NB": ("HST", 0.15),      "NL": ("HST", 0.15),      "PE": ("HST", 0.15),
    "QC": ("GST+QST", 0.14975),
}


def test_rate_table_matches_accountings_sheet():
    """Pinned to the sheet so a rate change has to be a deliberate edit here.
    SK sat at 5% for months because nothing compared the two."""
    from app.main import _PROVINCE_TAX_RATES
    assert _PROVINCE_TAX_RATES == RATE_SHEET


def test_saskatchewan_residual_matches_gst_plus_pst():
    """SK is the one province where we charge PST. At the old 5% the residual
    on an SK invoice matched nothing and the invoice read as unexplained."""
    inv = _base(province="SK", subtotal=1000.0, discount_amount=0.0,
                discrepancy=110.0)
    _annotate_tax_suggestion([inv])
    ts = inv.get("tax_suggestion")
    assert ts is not None
    assert ts["kind"] == "GST+PST"
    assert ts["rate"] == 0.11
    assert ts["amount"] == 110.0


def test_saskatchewan_at_gst_only_is_no_longer_suggested():
    """The mirror of the above: 5% of net on an SK invoice is not what we
    charge, so it must not be offered as the explanation for a residual."""
    inv = _base(province="SK", subtotal=1000.0, discount_amount=0.0,
                discrepancy=50.0)
    _annotate_tax_suggestion([inv])
    assert inv.get("tax_suggestion") is None


def test_bc_stays_gst_only_while_crstl_still_sends_pst():
    """Crstl has been asked to stop sending an ST segment on BC dropship. Until
    that lands a BC invoice can carry 12%, and that residual must NOT resolve
    to a tidy suggestion: 5% is what we charge, and offering it as the
    explanation for a 12% residual would be the hint asserting something false.

    This is the dashboard hint only. It says nothing about the workbook, which
    reads TXI and reconciles such an invoice to the cent -- see the note on
    _PROVINCE_TAX_RATES.
    """
    inv = _base(province="BC", subtotal=1000.0, discount_amount=0.0,
                discrepancy=120.0)
    _annotate_tax_suggestion([inv])
    assert inv.get("tax_suggestion") is None
