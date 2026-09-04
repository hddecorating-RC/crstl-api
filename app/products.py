"""Which product line an ordered item belongs to: drapery or blinds.

Source of truth: HD's vendor item numbers, confirmed against our own
catalogue. They are the only product identifier the EDI carries on every
Dropship line, and they separate the two lines cleanly:

    Blinds    a three-digit width code plus a two-letter product code
              138VB48D36WHTC  (1 3/8" vinyl blind)
              020FW...        (faux wood)

    Drapery   a five-digit style number in the 7xxxx block, then
              dash-separated colour and size blocks -- three of them, or
              four, or five, depending on the style
              72136-109-001   71555-109-644-108   72318-109-52-84-404

Read from the 850, not the 810. An 810 carries no vendor item number at all:
DSD sends a UPC and a description, Dropship sends neither. The 850 is fetched
already, for the ship-to province, so this costs no extra call.

DSD is the exception, and app/report.py handles it rather than this module:
DSD 850s carry no vendors_item_number either -- Crstl's CSV export has the
field, generic_json_edi does not -- and every DSD item HD has ordered is
drapery. Blinds reach us only by Dropship.
"""
import re

BLIND = "Blind"
DRAPE = "Drape Panel"
MIXED = "Mixed"
UNKNOWN = "Unknown"

BLIND_ITEM = re.compile(r"^\d{3}(VB|FW)", re.I)
DRAPE_ITEM = re.compile(r"^7\d{4}(-\d+)+$")


def product_of(vendor_item: str) -> str:
    """The product line one item belongs to, or "" when its number fits
    neither shape. Unrecognised is returned empty rather than guessed at, so a
    new product code shows up as Unknown on the report instead of being filed
    under whichever line it resembles."""
    item = str(vendor_item or "").strip()
    if BLIND_ITEM.match(item):
        return BLIND
    if DRAPE_ITEM.match(item):
        return DRAPE
    return ""


def vendor_items_in(detail: dict) -> list:
    """Every vendor item number on an 850, in line order."""
    lines = (((detail.get("file") or {}).get("generic_json_edi") or {})
             .get("detail") or {}).get("baseline_item_data_loop") or []
    items = [str((line.get("baseline_item_data") or {}).get("vendors_item_number") or "")
             for line in lines]
    return [i for i in items if i]


def label_for(vendor_items) -> str:
    """The one value the Product column shows for a whole invoice.

    Unrecognised items are dropped rather than counted as a third kind: a PO
    of blinds carrying one item we cannot place is still a PO of blinds, and
    calling it Mixed would send accounting looking for drapery that is not
    there. Only when nothing at all is recognised does the column say so.
    """
    kinds = {k for k in (product_of(i) for i in vendor_items) if k}
    if not kinds:
        return ""
    return kinds.pop() if len(kinds) == 1 else MIXED
