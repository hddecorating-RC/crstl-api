"""DSD pickup numbers: copy PRO + RTS from the Accepted 856 onto the Finale shipment.

The DSD flow, as the warehouse runs it (settled with Ritchie 2026-09-15):

    pack in Finale  ->  ASN in Crstl (PRO + RTS + planned pickup, typed once from
    HD's system)  ->  HD accepts the 856  ->  truck comes (<= 48 business hours)
    ->  warehouse clicks "Ship shipment" in Finale  ->  810 Accepted  ->  invoice.

The Ship click IS the pickup confirmation and stays a human step (the API cannot
pack or ship -- 403). What this module removes is the SECOND keying: retyping the
PRO into the shipment's tracking field at ship time. When an ASN is Accepted, the
shipment is still open (INPUT or PACKED), so the pass writes

    trackingCode = PRO (bill_of_lading_number, 3200...)
    publicNotes  = "RTS <carrier_reference_number>" (6100...)

exactly where the warehouse puts them by hand (40864264-1 is the reference), and
nothing else -- in particular never shipDateEstimated, which the Ship dialog would
adopt as the real ship date. Both fields survive the manual Ship click (proven on
TEST_0005-3). The invoice engine then does the rest on 810 Accepted + SHIPPED.

Not a digest concern: this is a warehouse convenience; alerts/monitoring come later.
"""
from app.netsuite_push import select_for_automation

RTS_PREFIX = "RTS "
DSD_FLAVOR = "Direct Store Delivery (DSD)"   # Crstl's trading_partner_flavor on the 856 listing
OPEN = ("SHIPMENT_INPUT", "SHIPMENT_PACKED")
CANCELLED = "SHIPMENT_CANCELLED"


def is_dsd_asn(asn: dict) -> bool:
    """An 856 is a DSD pickup when it carries a PRO (bill of lading). Dropship and
    Wholesale ASNs carry courier tracking instead and never a BOL."""
    return bool(str(asn.get("pro") or "").strip())


def rts_note(rts: str) -> str:
    return f"{RTS_PREFIX}{rts}" if rts else ""


def plan_shipment(asn: dict, shipment: dict) -> dict:
    """What to do with ONE Finale shipment for this ASN: {action, fields, reason}.
    action: write | equal | shipped | cancelled. Pure -- no I/O."""
    status = str(shipment.get("statusId") or "")
    pro, rts = str(asn.get("pro") or ""), str(asn.get("rts") or "")
    have_pro, have_note = str(shipment.get("trackingCode") or ""), str(shipment.get("publicNotes") or "")
    fields = {}
    if pro and have_pro != pro:
        fields["trackingCode"] = pro
    # The warehouse writes the bare number ("6100994307"); we write "RTS 6100994307".
    # Either counts as present -- the number is what matters.
    if rts and rts not in have_note:
        fields["publicNotes"] = rts_note(rts)
    if status == CANCELLED:
        return {"action": "cancelled", "fields": {}, "reason": "shipment cancelled"}
    if not fields:
        return {"action": "equal", "fields": {}, "reason": "PRO/RTS already on the shipment"}
    if status not in OPEN:
        # Shipped (or delivered) before the pass got to it: the warehouse's own entry
        # stands; an edit on a shipped shipment is unproven and not worth the risk.
        return {"action": "shipped", "fields": {},
                "reason": f"already {status.replace('SHIPMENT_', '').lower()} with "
                          f"tracking {have_pro or '-'} (ASN PRO {pro})"}
    return {"action": "write", "fields": fields, "reason": ""}


def select_dsd_asns(states: dict[str, dict], done_ids, *, created_after: str | None,
                    created_within_days: int | None, max_per_run: int | None) -> tuple[list[str], str | None]:
    """The ASN ids the automated pass should look at: Accepted, DSD by Crstl's own
    flavor label (so Dropship/Wholesale 856s cost no detail fetch), created on/after
    the floor and inside the rolling window, no receipt yet, under the cap (the shared
    automation guards -- see select_for_automation). Returns (ids, blocked_reason)."""
    candidates = [{"transaction_id": aid, "created_at": s.get("created_at"), "po_number": s.get("po_number")}
                  for aid, s in states.items()
                  if s.get("state") == "Accepted" and s.get("flavor", DSD_FLAVOR) == DSD_FLAVOR]
    todo = [c["transaction_id"] for c in candidates if c["transaction_id"] not in set(done_ids or [])]
    chosen, blocked = select_for_automation(candidates, todo, created_after=created_after,
                                            created_within_days=created_within_days, max_per_run=max_per_run)
    return [c["transaction_id"] for c in chosen], blocked


def push_dsd_prefill(asns: list[dict], *, live: bool = False, only: list[str] | None = None,
                     limit: int | None = None, client=None) -> dict:
    """Write PRO/RTS onto the open Finale shipments of these Accepted DSD ASNs (live)
    or report what would be written (dry). One row per ASN:
      status: prefilled | would_prefill | skipped_equal | skipped_shipped |
              skipped_no_order | skipped_no_shipment | skipped_not_dsd |
              skipped_not_accepted | skipped_done | failed
    Receipts (app.tracking.finale_shipments) are written for the terminal outcomes
    only -- prefilled / equal / shipped -- so an ASN whose order or shipment has not
    reached Finale yet is retried next pass.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    from app import tracking
    if only is not None:
        wanted = {str(x) for x in only}
        asns = [a for a in asns if str(a.get("asn_id")) in wanted]
    if limit is not None:
        asns = asns[:limit]
    if client is None:
        from app.finale import FinaleClient, FinaleUnavailable
        if FinaleClient.configured():
            client = FinaleClient()
        elif live:
            raise FinaleUnavailable("FINALE_* credentials not set")

    existing = tracking.get_finale_shipments([str(a.get("asn_id")) for a in asns])
    results: list[dict] = []
    counts = {k: 0 for k in ("prefilled", "would_prefill", "skipped_equal", "skipped_shipped", "skipped_no_order",
                             "skipped_no_shipment", "skipped_not_dsd", "skipped_not_accepted", "skipped_done", "failed")}

    def finish(row, status, **extra):
        row.update({"status": status, **extra})
        counts[status] += 1
        results.append(row)

    for asn in asns:
        aid = str(asn.get("asn_id") or "?")
        row = {"asn_id": aid, "po_number": asn.get("po_number"), "pro": asn.get("pro"), "rts": asn.get("rts"),
               "pickup_date": asn.get("pickup_date"), "shipments": [], "error": None}
        prior = existing.get(aid)
        if prior:
            finish(row, "skipped_done", error=f"already handled ({prior.get('status')}, shipment {prior.get('shipment_id') or '-'})"); continue
        if str(asn.get("state") or "Accepted") != "Accepted":
            finish(row, "skipped_not_accepted", error=f"ASN state {asn.get('state')}"); continue
        if not is_dsd_asn(asn):
            # Permanent property of the ASN: receipt it (live) so the pass never
            # re-reads a Dropship 856.
            if live:
                tracking.record_finale_shipment(aid, row["po_number"], None, None, None, "skipped_not_dsd")
            finish(row, "skipped_not_dsd", error="no bill of lading on the ASN (not a DSD pickup)"); continue
        if client is None:
            finish(row, "would_prefill", error="no Finale client (dry)"); continue
        try:
            order = client.get_order(str(asn.get("po_number") or ""))
            if order is None:
                finish(row, "skipped_no_order", error=f"no Finale order {asn.get('po_number')} yet -- will retry"); continue
            shipments = [s for s in client.order_shipments(order) if s.get("statusId") != CANCELLED]
            if not shipments:
                finish(row, "skipped_no_shipment", error="no shipment on the Finale order yet -- will retry"); continue
            plans = [(s, plan_shipment(asn, s)) for s in shipments]
            row["shipments"] = [{"shipment_id_user": s.get("shipmentIdUser") or s.get("shipmentId"),
                                 "status": s.get("statusId"), "action": p["action"], "fields": p["fields"],
                                 "reason": p["reason"]} for s, p in plans]
            writes = [(s, p) for s, p in plans if p["action"] == "write"]
            first = shipments[0]
            sid = str(first.get("shipmentIdUser") or first.get("shipmentId") or "")
            if not writes:
                # Terminal either way; the receipt (live only) stops the pass re-reading
                # this ASN every 15 minutes. A dry run leaves no trace.
                if all(p["action"] == "equal" for _, p in plans):
                    status, why = "skipped_equal", "PRO/RTS already on the shipment"
                else:
                    status, why = "skipped_shipped", "; ".join(p["reason"] for _, p in plans if p["action"] == "shipped")
                if live:
                    tracking.record_finale_shipment(aid, row["po_number"], sid, row["pro"], row["rts"], status)
                finish(row, status, error=why); continue
            if not live:
                finish(row, "would_prefill"); continue
            for s, p in writes:
                client.update_shipment(str(s.get("shipmentUrl")), p["fields"])
            tracking.record_finale_shipment(aid, row["po_number"], sid, row["pro"], row["rts"], "prefilled")
            finish(row, "prefilled", shipment_id_user=sid)
        except Exception as exc:   # one bad ASN must not stop the batch
            finish(row, "failed", error=str(exc))

    return {"mode": "live" if live else "dry", "results": results,
            "summary": {"candidates": len(asns), **counts}}
