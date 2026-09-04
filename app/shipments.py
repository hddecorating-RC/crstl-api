"""What the 856 ASN says about when an order moved -- and what it does not.

The 810 carries no date but invoice_date, so this comes from the ASN, joined
to the invoice on the PO number app/report.py already uses for Province and
Product. One more index over the same key, not a new join.

THE ASN DATE MEANS DIFFERENT THINGS BY FLAVOR. This is the whole reason the
workbook carries two columns rather than one:

    Dropship / Wholesale   the real ship date. It equals the ASN notice date
                           on every ASN on record -- the goods leave the day
                           the notice goes out.

    DSD                    a SCHEDULED PICKUP date, not a ship date. HD
                           requires one to raise the ASN, and the goods
                           actually ship 3-4 days later. Measured over the 50
                           dated DSD ASNs on record, the median gap between
                           sending the ASN and this date is +3 days.

The actual DSD ship date is NOT in the 856. A DSD ASN carries exactly two
dates -- heading.ship_notice_date and shipments[].shipment_date -- and no
other date-like field anywhere in the payload. It lives in Finale, which this
service does not yet talk to. So a DSD row's Ship Date is deliberately left
blank rather than filled with the pickup date: a blank is a gap, and a pickup
date under a "Ship Date" heading would be an error that reads like data.

Two further traps, both confirmed against every ASN on record:

  * The field name follows the flavor. Dropship and Wholesale spell it
    `shipped_date`; DSD spells it `shipment_date`. Reading only one silently
    blanks the other channel.

  * `heading.ship_notice_date` is never a fallback. On every Wholesale ASN it
    is the stub 2008-11-10, which would date real 2026 shipments to 2008.

Only Accepted ASNs count. Drafts are the warehouse's superseded resubmissions
-- 9 POs carry an Accepted ASN beside a Draft one -- and counting them would
let a withdrawn date win. Same Accepted-only rule the report applies to 810s.
"""

SHIPPED = "Accepted"


def asn_dates_in(detail: dict) -> list:
    """Every distinct movement date on one 856, sorted.

    An ASN can carry several shipments. De-duplicated and sorted so a caller
    can tell a split shipment from a repeated one.
    """
    shipments = (((detail.get("file") or {}).get("generic_json_edi") or {})
                 .get("detail") or {}).get("shipments") or []
    dates = {str(s.get("shipment_date") or s.get("shipped_date") or "").strip()
             for s in shipments}
    return sorted(d for d in dates if d)


def asn_date_label(dates) -> str:
    """The one value a date column shows for a whole invoice.

    A split shipment is shown as its range rather than its first date, which
    would date the invoice earlier than half of what it covers. The wording
    matches the Summary sheet's window label, so a reader meets the same
    "A to B" shape in both places. Nothing on file stays blank -- an invoice
    whose ASN has not arrived is a real state, and naming it would be a claim.
    """
    dates = sorted({d for d in dates if d})
    if not dates:
        return ""
    return dates[0] if dates[0] == dates[-1] else f"{dates[0]} to {dates[-1]}"


def asn_date_index(details) -> dict:
    """{po_number: date label} over a set of 856 details.

    POs with no dated Accepted ASN are left out rather than mapped to "", so a
    caller merging this into the PO index cannot overwrite a known date with a
    blank.
    """
    by_po = {}
    for detail in details:
        meta = detail.get("metadata") or {}
        if str((meta.get("state") or {}).get("value") or "") != SHIPPED:
            continue
        po = str(meta.get("source_document_reference_id") or "")
        dates = asn_dates_in(detail)
        if not po or not dates:
            continue
        by_po.setdefault(po, set()).update(dates)
    return {po: asn_date_label(dates) for po, dates in by_po.items()}


def merge_asn_dates(po_index: dict, asn_dates: dict) -> dict:
    """Fold an ASN-date index into the PO index the workbook reads, in place.

    Stored as `asn_date` -- the raw date, before the flavor decides whether it
    is a ship date or a pickup date. app/report.py makes that call, because
    only it knows the flavor.

    A PO with an ASN but no 850 on file still gets an entry: its date is real
    even though its Province and Product are not known, and dropping it would
    hide a shipment because a different document is missing.
    """
    for po, label in (asn_dates or {}).items():
        po_index.setdefault(po, {})["asn_date"] = label
    return po_index
