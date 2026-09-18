"""Dropship pre-fill: carrier + tracking onto the PACKED Finale shipment.

Ritchie, 2026-09-17: the warehouse clicks "Ship shipment" in Finale at end of day,
off what is packed -- "if it's packed, it's ready to go" -- so the actual ship date
becomes a human confirmation that the goods were really collected, instead of the
label date stamped hours earlier. Finale's ship date is immutable once set (proven
2026-09-17), so the only way to make it true is to not set it early.

To allow that, the Finale ShipStation connection's "Pull Sales to Finale" warehouse
row was changed to "Do not update stock levels", which stops it marking the shipment
shipped -- and with it, stops it writing the tracking number. This pass writes the
tracking and the carrier onto the still-PACKED shipment so the warehouse never types
them, and deliberately does NOT ship anything.

It also REOPENS the order when the connection has closed it. Verified 2026-09-17 on
TEST_0007: the connection still completes the Finale order on the ship event (within
seconds to a few minutes) even though it no longer ships the shipment, and a closed
order makes the shipment "not editable or actionable" -- so the warehouse could not
click Ship at end of day, and we could not write to the shipment either. A reopen
sticks: the connection did not re-close one across ten minutes and two poll cycles.
The invoice pass would also reopen it, but only for an order it happens to be
processing and only if it runs first, so this pass does it itself rather than relying
on that ordering.

Strict by design (Ritchie: start strict and see what it reports): it writes only when
the PO has exactly ONE live Finale shipment and exactly ONE non-voided ShipStation
label. Split shipments and re-labels are reported, never guessed at.
"""
OPEN = ("SHIPMENT_INPUT", "SHIPMENT_PACKED")
CANCELLED = "SHIPMENT_CANCELLED"


def po_of(shipment: dict) -> str:
    """The HD PO number a Finale shipment belongs to, from its primaryOrderUrl."""
    return str(shipment.get("primaryOrderUrl") or "").rstrip("/").rsplit("/", 1)[-1]


def live_shipments_by_po(rows: list[dict]) -> dict[str, list[dict]]:
    """{po: [non-cancelled shipments]} over a Finale shipment LISTING. Cancelled
    ones are dropped here so the one-shipment rule is not tripped by a shipment the
    warehouse built, cancelled and rebuilt (538873472 did exactly that)."""
    out: dict[str, list[dict]] = {}
    for r in rows:
        if str(r.get("statusId") or "") == CANCELLED:
            continue
        po = po_of(r)
        if po:
            out.setdefault(po, []).append(r)
    return out


def plan_prefill(shipment: dict, tracking: str, carrier_url: str | None) -> dict:
    """What to do with ONE packed Finale shipment: {action, fields, reason}.
    action: write | equal | shipped. Pure -- no I/O. Never sets a status or a date:
    shipping is the warehouse's click, and shipDateEstimated is read by nothing."""
    status = str(shipment.get("statusId") or "")
    fields = {}
    if tracking and str(shipment.get("trackingCode") or "") != tracking:
        fields["trackingCode"] = tracking
    if carrier_url and str(shipment.get("carrierPartyUrl") or "") != carrier_url:
        fields["carrierPartyUrl"] = carrier_url
    if status not in OPEN:
        # Already shipped (the warehouse got there first, or this ran late): the
        # shipment is locked and its date is the one that counts. Leave it.
        return {"action": "shipped", "fields": {},
                "reason": f"already {status.replace('SHIPMENT_', '').lower()}"}
    if not fields:
        return {"action": "equal", "fields": {}, "reason": "carrier/tracking already on the shipment"}
    return {"action": "write", "fields": fields, "reason": ""}


def push_dropship_prefill(shipment_rows: list[dict], labels: list[dict], *, live: bool = False,
                          only: list[str] | None = None, limit: int | None = None, client=None,
                          carrier_url: str | None = None, created_after: str | None = None,
                          max_per_run: int | None = None, reopen: bool = True) -> dict:
    """Write carrier + tracking onto the packed Finale shipment of each dropship
    label (live), or report what would be written (dry). One row per PO:
      status: prefilled | would_prefill | skipped_equal | skipped_shipped |
              skipped_no_shipment | skipped_ambiguous | skipped_floor | failed
    A row also carries `reopened` when the closed order had to be reopened first --
    which is also what makes the shipment actionable for the warehouse's end-of-day
    Ship click, so it happens even when there is nothing to write.

    `shipment_rows` is a Finale shipment LISTING (one request, no per-order reads);
    `labels` are ShipStation v1 shipment records for the dropship store. `max_per_run`
    counts the shipments about to be written; over it the run is refused before the
    first write.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    by_po = live_shipments_by_po(shipment_rows)

    # One PO can carry several labels (a re-label, or a split). Voided ones never count.
    labels_by_po: dict[str, list[dict]] = {}
    for lb in labels:
        if lb.get("voided"):
            continue
        po = str(lb.get("orderNumber") or "")
        if po:
            labels_by_po.setdefault(po, []).append(lb)

    pos = sorted(labels_by_po)
    if only is not None:
        wanted = {str(x) for x in only}
        pos = [p for p in pos if p in wanted]
    if limit is not None:
        pos = pos[:limit]

    counts = {k: 0 for k in ("prefilled", "would_prefill", "skipped_equal", "skipped_shipped",
                             "skipped_no_shipment", "skipped_ambiguous", "skipped_floor", "failed")}
    results: list[dict] = []
    writers: list[tuple[dict, dict, dict]] = []

    def finish(row, status, **extra):
        row.update({"status": status, **extra}); counts[status] += 1; results.append(row)

    for po in pos:
        lbs = labels_by_po[po]
        row = {"po_number": po, "labels": len(lbs), "tracking": None, "shipment_id_user": None,
               "fields": {}, "error": None}
        newest = max(lbs, key=lambda x: str(x.get("createDate") or ""))
        created = str(newest.get("createDate") or "")[:10]
        if created_after and (not created or created < created_after):
            finish(row, "skipped_floor", error=f"label {created or '?'} before floor {created_after}"); continue
        if len(lbs) != 1:
            finish(row, "skipped_ambiguous",
                   error=f"{len(lbs)} live labels for this PO -- not guessing which shipment they belong to"); continue
        row["tracking"] = str(newest.get("trackingNumber") or "") or None
        ships = by_po.get(po) or []
        if not ships:
            finish(row, "skipped_no_shipment", error="no live Finale shipment yet -- will retry"); continue
        if len(ships) != 1:
            finish(row, "skipped_ambiguous",
                   error=f"{len(ships)} live Finale shipments -- not guessing which carries the label"); continue
        listed = ships[0]
        row["shipment_id_user"] = listed.get("shipmentIdUser") or listed.get("shipmentId")
        if str(listed.get("statusId") or "") not in OPEN:
            finish(row, "skipped_shipped",
                   error=f"already {str(listed.get('statusId')).replace('SHIPMENT_', '').lower()}"); continue
        try:
            # The listing carries no trackingCode/carrierPartyUrl, so the decision to
            # write needs the full record. Only ever for a packed dropship shipment.
            full = client.get_shipment(str(listed.get("shipmentUrl")))
            # A closed order makes the shipment unwritable AND unshippable by the
            # warehouse, so reopen it whether or not we have anything to write.
            order = client.get_order(po)
            if reopen and order is not None and str(order.get("statusId") or "") == "ORDER_COMPLETED":
                if live:
                    client.reopen_order(order)
                row["reopened"] = True
        except Exception as exc:  # noqa: BLE001 -- one bad read must not stop the batch
            finish(row, "failed", error=f"Finale read failed: {exc}"); continue
        p = plan_prefill(full, row["tracking"] or "", carrier_url)
        row["fields"], row["error"] = p["fields"], (p["reason"] or None)
        if p["action"] == "equal":
            # Nothing to write, but the reopen above may still have been the point.
            finish(row, "skipped_equal"); continue
        if p["action"] == "shipped":
            finish(row, "skipped_shipped"); continue
        finish(row, "would_prefill")
        writers.append((row, full, p["fields"]))

    blocked = None
    if max_per_run is not None and len(writers) > max_per_run:
        blocked = f"{len(writers)} shipments to write exceeds max_per_run {max_per_run} -- nothing written"
    for row, full, fields in (writers if (live and not blocked) else []):
        counts["would_prefill"] -= 1
        try:
            client.update_shipment(str(full.get("shipmentUrl")), fields)
            row["status"] = "prefilled"; counts["prefilled"] += 1
        except Exception as exc:  # noqa: BLE001
            row.update(status="failed", error=str(exc)); counts["failed"] += 1

    out = {"mode": "live" if live else "dry", "results": results,
           "summary": {"candidates": len(pos), **counts}}
    if blocked:
        out["blocked"] = blocked
    return out
