"""EDI 810 SAC (Service/Promotion/Allowance/Charge) code classification.

Source of truth: Home Depot Canada 810 Invoice Specification v6.10 (2010-06-21),
pages 32–33, EXCEPT the tax codes: those follow HD's current VAT matrix, CMP Vendor
Best Practice Document v9-12-2025 p.23 (reference/). The 2010 spec's province lists
predate BC leaving HST and PEI joining it, and it had no H770. Where HD annotates a
standard X12 code with a specific business use, we use HD's business name.

Categories:
  allowance — indicator "A", reduces invoice net (deductions we grant HD)
  discount  — indicator "A", also a reduction but conceptually a price cut
              rather than a service allowance
  freight   — indicator "C", freight billed back to HD (increases payable)
  fee       — indicator "C", non-tax non-freight charges
  tax       — indicator "C", governmental tax (GST/HST/QST/ECO). These are
              summed into `tax_amount`, not `charge_amount`.

Codes not in HD's spec (like C300 seen empirically on dropship at 1.25%) are
handled by CLASSIFY_UNKNOWN so unmapped codes still flow through without
disappearing. Add newly-observed codes here as HD confirms them.
"""

# Per-code metadata. Only 22 codes are documented in HD's spec; more may appear.
CODE_META: dict[str, dict] = {
    # ── Allowances (indicator A) ────────────────────────────────────────────
    "C000": {"label": "Defective Allowance",       "category": "allowance"},
    "D240": {"label": "Freight Allowance",         "category": "allowance"},
    "E180": {"label": "Labor Repair/Return",       "category": "allowance"},
    "E210": {"label": "Labor Service",             "category": "allowance"},
    "F800": {"label": "Promotional Allowance",     "category": "allowance"},
    "F991": {"label": "Receiving",                 "category": "allowance"},
    "H000": {"label": "Special Allowance",         "category": "allowance"},
    "H090": {"label": "DC Handling Allowance",     "category": "allowance"},

    # ── Discounts (indicator A but semantically a price cut) ────────────────
    "C300": {"label": "Discount",                  "category": "discount"},   # not in HD spec; empirical 1.25% on dropship
    "E750": {"label": "New Store Discount",        "category": "discount"},
    "F810": {"label": "Promotional Discount",      "category": "discount"},
    "F910": {"label": "Quantity Discount",         "category": "discount"},
    "I170": {"label": "Trade Discount",            "category": "discount"},
    "I530": {"label": "Volume Discount",           "category": "discount"},

    # ── Freight (indicator C) ───────────────────────────────────────────────
    "D200": {"label": "Freight to Destination",    "category": "freight"},

    # ── Fees (indicator C, non-tax non-freight) ─────────────────────────────
    "H400": {"label": "Drop Charge",               "category": "fee"},

    # ── Taxes (indicator C) — HD-annotated meanings ─────────────────────────
    # D360: GST 5% -- AB, BC, MB, NT, NU, SK, YT, and the GST line on Quebec invoices.
    #       No PST anywhere: HD is PST-exempt on resale (CMP doc p.13, reject ED41P).
    "D360": {"label": "GST Tax",                   "category": "tax", "tax_kind": "GST"},
    # H770: HST -- ON 13%, NB/NL/PE 15%, NS 14% (since 2025-04-01). Until 2026-09-22 this
    #       was unmapped and read as a FEE, so Vaughan's HST showed under Fees.
    "H770": {"label": "HST Tax",                   "category": "tax", "tax_kind": "HST"},
    # H680: QST 9.975%, Quebec ONLY, always alongside D360. Anywhere else HD rejects the
    #       invoice (E995) -- which is how SK PST sent as H680 was caught.
    "H680": {"label": "QST Tax",                   "category": "tax", "tax_kind": "QST"},
    # H850, F240, G090, G100: BC ECO Tax at different container sizes
    "F240": {"label": "BC Eco Tax (≤250ml)",       "category": "tax", "tax_kind": "ECO"},
    "G090": {"label": "BC Eco Tax (250ml–1L)",     "category": "tax", "tax_kind": "ECO"},
    "G100": {"label": "BC Eco Tax (1L–5L)",        "category": "tax", "tax_kind": "ECO"},
    "H850": {"label": "BC Eco Tax (5L–23L)",       "category": "tax", "tax_kind": "ECO"},
}


def classify(code: str, indicator: str) -> dict:
    """Return metadata for a SAC code. Falls back to a safe default keyed on
    the indicator for codes not in the spec — this way an undocumented code
    still shows up in the UI/CSV as an allowance or a fee (never disappears)."""
    meta = CODE_META.get(code)
    if meta:
        return meta
    if indicator == "A":
        return {"label": f"Allowance (unmapped)", "category": "allowance"}
    if indicator == "C":
        return {"label": f"Charge (unmapped)",    "category": "fee"}
    return {"label": f"Unknown ({indicator})",    "category": "fee"}


def label(code: str) -> str:
    """Human label for a code, falling back to the raw code for unmapped ones."""
    meta = CODE_META.get(code)
    return f"{meta['label']} ({code})" if meta else f"Code {code}"
