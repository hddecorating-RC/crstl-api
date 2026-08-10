# Reported Tax (`tax_information`) — Design Spec
**Date:** 2026-08-10
**Status:** Draft — awaiting review

## Overview

Crstl now populates `summary.tax_information` on the 810 detail response, exposing the tax HD reports per invoice. Nothing in our pipeline reads it. This spec wires it in.

The governing principle is the one already stated at `app/crstl.py:190` — report what HD sends, never derive a number that hides a data problem. Reading `tax_information` *strengthens* that contract: it replaces a heuristic guess with HD's own figure, and lets the discrepancy calculation surface the invoices that genuinely do not add up.

Secondary goal, and the reason this is worth doing carefully: the same wiring makes three classes of mistake visible — wrong tax mapping, wrong tax rate, and discounts reported but not applied.

### System of record

**Crstl is authoritative.** It is what is ultimately communicated to HD, so its numbers are the numbers, and this pipeline reports them rather than correcting them.

The value of the cross-checks is therefore not to produce a better figure — it is to surface *where numbers differ*. Three kinds of divergence matter, in descending confidence:

1. **Internally inconsistent within one Crstl invoice** — the `C300` discount applied to the tax base but not the total (§5). Provable from a single document.
2. **Between two transmissions of the same invoice** — `INV537863522` arrived twice with different discount, tax, and total. Provable from Crstl alone.
3. **Between Crstl and an off-platform source** — the sampled Rithum invoice computed GST on the gross subtotal where all 42 Crstl invoices use the net base. Cannot be adjudicated from Crstl data; flagged for the Rithum cycle.

Where a cross-check and Crstl disagree, the cross-check is what yields — it annotates, never overwrites.

---

## What the API actually returns

Location: `file.generic_json_edi.summary.tax_information` — a sibling of the SAC loop we already parse, inside the EDI translation itself.

```json
"tax_information": [
  {"tax_type_code": "CG", "monetary_amount": "2.12"}
]
```

Survey of all 113 live 810s on 2026-08-10:

| Code | n | Implied rate (net base) | Reading |
|---|---|---|---|
| `CG` | 21 | 4.93–5.01% | GST 5% |
| `VA` | 18 | 12.83–13.03%, one 14.01% | HST (ON 13%, NS 14%) |
| `CG`+`ST` | 3 | 14.94–17.31% combined | GST + QST (Quebec) |
| `null` | 1 | — | `INV2013003`, $0 invoice |

**These codes are not in the HD Canada 810 specification.** That document defines tax solely through SAC codes (D360, H680, H850). In X12, `VA` is Value Added Tax and `ST` is State Sales Tax; `CG` is not a Canadian GST code. HD is using them loosely and Crstl maps them through.

Consequence for the design: **we do not translate these codes into our `GST`/`HST_QST` vocabulary.** The rate-to-meaning mapping above is an inference from 42 invoices, not a specification. It belongs in an advisory cross-check, not in the reported data.

Evidence this is HD's data rather than a Crstl-side computation: on 35 of 43 invoices the sum equals our discrepancy exactly, but on 8 it does not. A synthesized residual would match all 43.

---

## Dropship and DSD invoice differently

The two channels are mutually exclusive in how they carry tax. This is not a variant of one model; it is two models sharing a schema.

| | Dropship (60) | DSD (51) | Wholesale (2) |
|---|---|---|---|
| Tax via TXI | **43** | 0 | 0 |
| Tax via SAC | 0 | **11** | 0 |
| No tax at all | 17 | 40 | 2 |
| Has allowance/discount | 25 | **51 (all)** | 0 |
| Has freight/fee | **0 (never)** | 3 | 0 |
| Unreconciled today | **42/60** | 4/51 | 0/2 |
| Unreconciled after this change | **8/60** | 4/51 | 0/2 |

No invoice carries both SAC tax and TXI tax, so there is no double-counting risk today.

Two comments in `app/crstl.py:217-222` are now stale and must be corrected as part of this work: Dropship tax is no longer "baked into the total" (it is reported explicitly), and neither Wholesale invoice carries the "full tax SAC codes" the comment claims.

---

## Design

### 1. Parse verbatim

`_extract_invoice_fields` reads the array into a new field, codes untouched:

```python
"reported_taxes": [{"code": "CG", "amount": 2.12}]
```

A missing or null `tax_type_code` is preserved as `None` — never guessed. A malformed `monetary_amount` parses through the existing `_parse_float`, yielding 0.0 rather than raising.

### 2. Tax and discrepancy

Reported tax is reported data, so it counts:

```
tax_amount = SAC tax + reported tax
```

Summing rather than choosing means that if HD ever sends both, the invoice shows the total instead of one silently winning. `tax_breakdown` gains the raw TXI codes as keys (`{"CG": 2.12}`) alongside existing SAC kinds — two vocabularies, each honestly labelled, no forced merge.

Observed TXI codes (`CG`, `VA`, `ST`) do not collide with existing SAC kinds (`GST`, `HST_QST`, `ECO`). Should a future code collide, the two amounts are summed under that key — consistent with how SAC entries of the same kind already accumulate at `crstl.py:173`, and the raw per-entry values remain available in `reported_taxes` regardless.

`discrepancy` recomputes with tax included:

```
computed_total = subtotal − allowance − discount + freight + fee + tax_amount
discrepancy    = total_amount − computed_total
```

Effect: 35 Dropship invoices go to 0.00 because they genuinely reconcile, and 8 keep a residual that is now a real anomaly rather than a known blind spot.

### 3. Flavor-aware expectations

Add `tax_channel` describing where tax was found: `"reported"` (TXI), `"sac"`, or `"none"`. The cross-check then asserts what each channel should look like and flags departures:

- **Dropship** — expects TXI. SAC tax, or any freight/fee, is unexpected.
- **DSD** — expects SAC. A DSD invoice carrying TXI is itself the anomaly.
- **Wholesale** — no established pattern (n=2); flag nothing, record the channel.

### 4. Rate cross-check — an educated guess, kept as a guess

`_annotate_tax_suggestion` currently fills a hole that no longer exists for these 43 invoices. It is repointed rather than deleted:

- Where tax **is** reported: compute the implied rate against the net base and compare with the ship-to province's expected rate. On divergence beyond tolerance, emit an advisory such as `CG at 5.0% — ON invoice, expected HST 13%`. This catches mis-mapped tax codes.
- Where tax is **not** reported (17 Dropship invoices, all currently reconciling): existing province-rate suggestion behaviour is unchanged.

The tax base is **net of allowance and discount**. This was tested against both candidate bases across all 42 non-zero invoices carrying tax: the net base reproduces the reported figure in 42/42 (3 net-only, 39 where no allowance exists so both bases agree), the gross base in 0.

This annotation never mutates `tax_amount`, `tax_breakdown`, or `discrepancy`.

### 5. New anomaly — discount reported but not applied

Four of the 8 remaining unreconciled invoices share one root cause. All carry SAC `C300 Discount`:

```
INV537858507   subtotal 43.00   C300 −0.54   TXI 2.12   total 45.12
  tax base:  43.00 − 0.54 = 42.46 × 5% = 2.12   ✓ discount applied
  total:     43.00 + 2.12       = 45.12         ✗ discount not applied
```

HD deducts the discount when computing tax, then bills as though it never existed. Same shape on `INV0383220` (0.69), `INV537863522` (0.57), `INV537849335` (0.30).

Detection: where `|total − (subtotal + tax_amount)| ≤ tolerance` **and** a discount or allowance is present, flag `discount_not_applied` with the amount. Tolerance follows the convention already used by the rate check at `main.py:182` — `max(0.02, 0.002 × subtotal)` — so cent-rounding on line items does not defeat the match. This is advisory and non-mutating, like the rate check.

### 6. Remaining unreconciled invoices

The other four are not tax problems and get no special handling — they stand as anomalies, which is correct:

- `INV2013003` — $0 total, −76.26 discrepancy, null tax code
- `INV4680671` / `INV7843674` — both $76.26, sharing that value with `INV2013003`; a credit/rebill trio
- `INV537653222` — TXI 2.13 against a 0.25 residual

---

## Blast radius

| Location | Change |
|---|---|
| `app/crstl.py:194` | `tax_amount` now includes reported tax |
| `app/crstl.py:202` | `discrepancy` recomputes |
| `app/crstl.py:217-222` | Stale flavor comments corrected |
| `app/main.py:155` | `_annotate_tax_suggestion` repointed to cross-check |
| `app/netsuite.py:66` | Merchandise line carries real tax for 43 Dropship invoices |
| `app/netsuite_csv.py:43` | Tax Amount column populated where it was 0 |
| `app/export.py:25,34,44` | Tax / Discrepancy / Suggested Tax columns shift |
| `app/static/index.html:331,351,381` | Suggestion-instead-of-tax branch inverts once tax is populated |

NetSuite exports for those 43 Dropship invoices will carry tax where they previously carried zero. This is the intended outcome and the main reason this spec needs sign-off before implementation.

---

## Testing

- **Parsing** (`test_crstl.py`): single entry, multi-entry (`CG`+`ST`), null `tax_type_code`, absent `tax_information`, non-numeric `monetary_amount`.
- **Tax and discrepancy**: reported tax reaches `tax_amount`; SAC and TXI sum when both present; a previously-unreconciled Dropship fixture lands at 0.00.
- **No double count**: a fixture with both SAC tax and TXI yields the sum, not either alone.
- **Flavor expectations**: DSD carrying TXI flags; Dropship carrying SAC tax flags; Dropship freight/fee flags.
- **Rate cross-check** (`test_tax_suggest.py`, extended): correct rate stays silent; `CG` at 5% on an ON invoice flags; net base used, not gross.
- **Discount not applied**: the `INV537858507` shape flags; a correctly-discounted invoice does not.
- **Regression**: existing suggestion behaviour intact where no tax is reported.

---

## Non-goals

Explicitly out of scope, each needing its own cycle:

1. **Duplicate invoices** — 4 invoice numbers arrive under multiple transaction IDs (`INV40831749` ×4 at $5,317.43, `INV40825499` ×3, `INV40855360` ×2, `INV537863522` ×2 *with differing content*). Double-payment exposure.
2. **Uninvoiced POs** — 107 Dropship POs ($6,288.60) and 18 DSD POs ($187,415.78) have no invoice in Crstl. The six known Rithum-billed POs are indistinguishable from never-billed ones in Crstl data.
3. **Rithum ingestion** — invoices raised directly in Rithum bypass this pipeline entirely. The sampled Rithum invoice computed GST on the **gross** subtotal (10.80 on 216.00) where all 42 Crstl invoices use the net base. Since Crstl is the system of record for what HD receives, this does not change the math here; it is a divergence to investigate alongside #2, and it can only be judged once Rithum data is available for comparison.
