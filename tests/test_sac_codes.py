from app.sac_codes import classify, label, CODE_META


def test_known_tax_codes_classified_as_tax_with_kind():
    assert classify("D360", "C")["category"] == "tax"
    assert classify("D360", "C")["tax_kind"] == "GST"
    assert classify("H680", "C")["category"] == "tax"
    assert classify("H680", "C")["tax_kind"] == "HST_QST"
    assert classify("H850", "C")["category"] == "tax"
    assert classify("H850", "C")["tax_kind"] == "ECO"


def test_bc_eco_tax_family_all_classified_together():
    for code in ("F240", "G090", "G100", "H850"):
        meta = classify(code, "C")
        assert meta["category"] == "tax"
        assert meta["tax_kind"] == "ECO"


def test_known_allowance_codes():
    for code in ("C000", "D240", "E210", "H000", "H090"):
        assert classify(code, "A")["category"] == "allowance"


def test_discount_vs_allowance_distinction():
    """Both indicator=A but different semantics — HD distinguishes trade
    discounts from operational allowances, so the UI/CSV should too."""
    assert classify("I170", "A")["category"] == "discount"    # Trade Discount
    assert classify("F910", "A")["category"] == "discount"    # Quantity Discount
    assert classify("H000", "A")["category"] == "allowance"   # Special Allowance
    assert classify("H090", "A")["category"] == "allowance"   # DC Handling Allowance


def test_freight_and_fee_distinct_from_tax():
    assert classify("D200", "C")["category"] == "freight"
    assert classify("H400", "C")["category"] == "fee"        # Drop Charge


def test_unmapped_code_a_indicator_falls_back_to_allowance():
    meta = classify("ZZZZ", "A")
    assert meta["category"] == "allowance"
    assert "unmapped" in meta["label"].lower()


def test_unmapped_code_c_indicator_falls_back_to_fee():
    """Safe default: unknown charge is a fee, NOT a tax. Never invent tax
    categorization for something HD hasn't documented — that would silently
    inflate tax_amount."""
    meta = classify("ZZZZ", "C")
    assert meta["category"] == "fee"


def test_label_includes_code_suffix_for_known():
    assert label("D360") == "GST Tax (D360)"
    assert label("H090") == "DC Handling Allowance (H090)"


def test_label_fallback_for_unknown():
    assert label("ZZZZ") == "Code ZZZZ"


def test_all_documented_codes_have_required_fields():
    """Every code in CODE_META must have both label and category; tax codes
    must also carry tax_kind so the breakdown works."""
    valid_categories = {"allowance", "discount", "freight", "fee", "tax"}
    for code, meta in CODE_META.items():
        assert "label" in meta, f"{code} missing label"
        assert meta["category"] in valid_categories, f"{code} has bad category {meta['category']}"
        if meta["category"] == "tax":
            assert "tax_kind" in meta, f"tax code {code} missing tax_kind"
