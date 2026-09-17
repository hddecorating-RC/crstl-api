"""DSD pickup numbers: copy PRO + RTS from the Accepted 856 onto the Finale shipment.

The DSD flow, as the warehouse runs it (settled with Ritchie 2026-09-15):

    pack in Finale  ->  ASN in Crstl (PRO + RTS + planned pickup, typed once from
    HD's system)  ->  HD accepts the 856  ->  truck comes (<= 48 business hours)
    ->  warehouse clicks "Ship shipment" in Finale  ->  810 Accepted  ->  invoice.

The Ship click IS the pickup confirmation and stays a human step (the API cannot
pack or ship -- 403). What this module removes is the SECOND keying: retyping the
PRO into the shipment's tracking field at ship time. When an ASN is Accepted, the
shipment is still open (INPUT or PACKED), so the pass writes

    trackingCode = PRO  (carrier_reference_number, 6100...)   -- printed as SCAC/PRO
    publicNotes  = "Routing: <RTS>" (bill_of_lading_number, 3200...)
    order field  = RTS  (user_10000, what the bill of lading prints)

(Which number is which was settled by the warehouse, 2026-09-16: RTS = 3200...,
PRO = 6100... These labels are for our Finale records only; the ASN is not touched.)

exactly where the warehouse puts them by hand (40864264-1 is the reference), and
nothing else -- in particular never shipDateEstimated, which the Ship dialog would
adopt as the real ship date. Both fields survive the manual Ship click (proven on
TEST_0005-3). The invoice engine then does the rest on 810 Accepted + SHIPPED.

Not a digest concern: this is a warehouse convenience; alerts/monitoring come later.
"""
from app.netsuite_push import select_for_automation

RTS_PREFIX = "Routing: "
DSD_FLAVOR = "Direct Store Delivery (DSD)"   # Crstl's trading_partner_flavor on the 856 listing
OPEN = ("SHIPMENT_INPUT", "SHIPMENT_PACKED")
CANCELLED = "SHIPMENT_CANCELLED"


def is_dsd_asn(asn: dict) -> bool:
    """An 856 is a DSD pickup when it carries a bill-of-lading number (the RTS,
    3200...). Dropship and Wholesale ASNs carry courier tracking instead and never
    a BOL."""
    return bool(str(asn.get("rts") or "").strip())


def rts_note(rts: str) -> str:
    return f"{RTS_PREFIX}{rts}" if rts else ""


def plan_shipment(asn: dict, shipment: dict, carrier_url: str | None = None, rts_on_order: bool = False) -> dict:
    """What to do with ONE Finale shipment for this ASN: {action, fields, reason}.
    action: write | equal | shipped | cancelled. Pure -- no I/O. `carrier_url` is the
    DSD carrier default (HDOC), written alongside PRO/RTS when the shipment's carrier
    differs, so the warehouse never picks it by hand. `rts_on_order` is accepted for
    compatibility and ignored: the RTS is written to the shipment notes REGARDLESS, so
    the shipment page shows it (Ritchie, 2026-09-16 -- "all shipping info on the same
    page"); the order's custom field (plan_order) is what the bill of lading prints."""
    status = str(shipment.get("statusId") or "")
    pro, rts = str(asn.get("pro") or ""), str(asn.get("rts") or "")
    have_pro, have_note = str(shipment.get("trackingCode") or ""), str(shipment.get("publicNotes") or "")
    fields = {}
    if pro and have_pro != pro:
        fields["trackingCode"] = pro
    # The warehouse may have written the bare number; we write "Routing: 3200416047".
    # Either counts as present -- the number is what matters.
    if rts and rts not in have_note:
        fields["publicNotes"] = rts_note(rts)
    if carrier_url and str(shipment.get("carrierPartyUrl") or "") != carrier_url:
        fields["carrierPartyUrl"] = carrier_url
    if status == CANCELLED:
        return {"action": "cancelled", "fields": {}, "reason": "shipment cancelled"}
    if not fields:
        return {"action": "equal", "fields": {}, "reason": "PRO/RTS" + ("/carrier" if carrier_url else "") + " already on the shipment"}
    if status not in OPEN:
        # Shipped (or delivered) before the pass got to it: the warehouse's own entry
        # stands; an edit on a shipped shipment is unproven and not worth the risk.
        return {"action": "shipped", "fields": {},
                "reason": f"already {status.replace('SHIPMENT_', '').lower()} with "
                          f"tracking {have_pro or '-'} (ASN PRO {pro})"}
    return {"action": "write", "fields": fields, "reason": ""}


ORDER_EDITABLE = ("ORDER_CREATED", "ORDER_LOCKED")


def plan_order(asn: dict, order: dict, mapping: dict) -> dict:
    """The sales-order custom fields this ASN should set: {attrName: value} for every
    mapped value (today: rts -> user_10000) that is missing or different on the order,
    plus `editable` (CREATED/LOCKED -- a completed order is never touched). Pure."""
    have = {e.get("attrName"): str(e.get("attrValue") or "") for e in (order.get("userFieldDataList") or []) if isinstance(e, dict)}
    fields = {}
    for key, attr in (mapping or {}).items():
        val = str(asn.get(key) or "").strip()
        if attr and val and have.get(attr, "") != val:
            fields[attr] = val
    return {"fields": fields, "editable": str(order.get("statusId") or "") in ORDER_EDITABLE}


def select_dsd_asns(states: dict[str, dict], done_ids, *, created_after: str | None,
                    created_within_days: int | None) -> list[str]:
    """The ASN ids the automated pass should look at: Accepted, DSD by Crstl's own
    flavor label (so Dropship/Wholesale 856s cost no detail fetch), created on/after
    the floor and inside the rolling window, no receipt yet (the shared automation
    guards -- see select_for_automation). The blast cap is applied by push_dsd_prefill
    to the shipments it is about to write, not to this pending set."""
    candidates = [{"transaction_id": aid, "created_at": s.get("created_at"), "po_number": s.get("po_number")}
                  for aid, s in states.items()
                  if s.get("state") == "Accepted" and (s.get("flavor") or DSD_FLAVOR) == DSD_FLAVOR]   # blank = unknown: fetch, let the BOL decide
    todo = [c["transaction_id"] for c in candidates if c["transaction_id"] not in set(done_ids or [])]
    chosen, _ = select_for_automation(candidates, todo, created_after=created_after,
                                      created_within_days=created_within_days, max_per_run=None)
    return [c["transaction_id"] for c in chosen]


def push_dsd_prefill(asns: list[dict], *, live: bool = False, only: list[str] | None = None,
                     limit: int | None = None, client=None, max_per_run: int | None = None,
                     refs: dict | None = None) -> dict:
    """Write PRO/RTS onto the open Finale shipments of these Accepted DSD ASNs (live)
    or report what would be written (dry). One row per ASN:
      status: prefilled | would_prefill | skipped_equal | skipped_shipped |
              skipped_no_order | skipped_no_shipment | skipped_not_dsd |
              skipped_not_accepted | skipped_done | failed
    Receipts (app.tracking.finale_shipments) are written for the terminal outcomes
    only -- prefilled / equal / shipped -- so an ASN whose order or shipment has not
    reached Finale yet is retried next pass. `max_per_run` counts the ASNs about to
    be written (would_prefill); over it the run is refused before the first write.
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

    # The DSD carrier default (config finale.carriers, OFF by default). Resolved once;
    # an unknown name is reported on every row and nothing carrier-related is written.
    from app.finale import wanted_carrier
    from app.netsuite_payload import load_refs
    fin = (refs or load_refs()).get("finale") or {}
    carrier = wanted_carrier(fin, "dsd", {})
    carrier_note = None
    if carrier["enabled"] and carrier["name"] and client is not None and asns:
        try:
            carrier = wanted_carrier(fin, "dsd", client.carrier_index())
        except Exception as exc:  # noqa: BLE001 -- PRO/RTS still go on; the carrier just does not
            carrier = {**carrier, "url": None, "reason": f"could not read Finale's carriers: {exc}"}
        carrier_note = carrier["reason"] or None
    carrier_url = carrier["url"] if carrier["enabled"] else None
    # Order custom fields (config finale.dsd_order_fields, OFF by default): the RTS
    # ALSO goes on the sales order so the bill of lading can print it; the shipment
    # notes get it either way (the shipment page has nowhere else to show it).
    of_cfg = fin.get("dsd_order_fields") or {}
    order_mapping = {k: v for k, v in of_cfg.items() if k in ("rts", "pro") and v} if of_cfg.get("enabled") else {}

    existing = tracking.get_finale_shipments([str(a.get("asn_id")) for a in asns])
    results: list[dict] = []
    writers: list[tuple] = []          # (row, [(shipment, plan)], shipment id, order, order fields) -- the set the cap counts
    counts = {k: 0 for k in ("prefilled", "would_prefill", "skipped_equal", "skipped_shipped", "skipped_no_order",
                             "skipped_no_shipment", "skipped_not_dsd", "skipped_not_accepted", "skipped_done", "failed",
                             "order_field_failed")}

    def finish(row, status, **extra):
        row.update({"status": status, **extra})
        counts[status] += 1
        results.append(row)

    for asn in asns:
        aid = str(asn.get("asn_id") or "?")
        row = {"asn_id": aid, "po_number": asn.get("po_number"), "pro": asn.get("pro"), "rts": asn.get("rts"),
               "pickup_date": asn.get("pickup_date"), "shipments": [], "error": None,
               "carrier": {"wanted": carrier["name"], "enabled": carrier["enabled"], "note": carrier_note}}
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
            plans = [(s, plan_shipment(asn, s, carrier_url, rts_on_order=bool(order_mapping.get("rts")))) for s in shipments]
            row["shipments"] = [{"shipment_id_user": s.get("shipmentIdUser") or s.get("shipmentId"),
                                 "status": s.get("statusId"), "action": p["action"], "fields": p["fields"],
                                 "reason": p["reason"]} for s, p in plans]
            writes = [(s, p) for s, p in plans if p["action"] == "write"]
            op = plan_order(asn, order, order_mapping) if order_mapping else {"fields": {}, "editable": True}
            order_fields = op["fields"] if op["editable"] else {}
            row["order_fields"] = {"fields": op["fields"], "order_status": order.get("statusId"),
                                   "note": None if (op["editable"] or not op["fields"]) else
                                   f"order is {order.get('statusId')}: custom fields not editable"}
            first = shipments[0]
            sid = str(first.get("shipmentIdUser") or first.get("shipmentId") or "")
            if not writes and not order_fields:
                # Terminal either way; the receipt (live only) stops the pass re-reading
                # this ASN every 15 minutes. A dry run leaves no trace.
                if all(p["action"] == "equal" for _, p in plans):
                    status, why = "skipped_equal", plans[0][1]["reason"]
                else:
                    status, why = "skipped_shipped", "; ".join(p["reason"] for _, p in plans if p["action"] == "shipped")
                if live:
                    tracking.record_finale_shipment(aid, row["po_number"], sid, row["pro"], row["rts"], status)
                finish(row, status, error=why); continue
            row["shipment_id_user"] = sid
            finish(row, "would_prefill")
            writers.append((row, writes, sid, order, order_fields))
        except Exception as exc:   # one bad ASN must not stop the batch
            finish(row, "failed", error=str(exc))

    blocked = None
    if max_per_run is not None and len(writers) > max_per_run:
        blocked = f"{len(writers)} shipments to write exceeds max_per_run {max_per_run} -- nothing written"
    for row, writes, sid, order, order_fields in (writers if (live and not blocked) else []):
        counts["would_prefill"] -= 1
        try:
            for s, p in writes:
                client.update_shipment(str(s.get("shipmentUrl")), p["fields"])
        except Exception as exc:   # one bad ASN must not stop the batch; no receipt -> retried next pass
            row.update(status="failed", error=str(exc)); counts["failed"] += 1
            continue
        if order_fields:
            try:
                client.set_order_user_fields(order, order_fields)
                row["order_fields"]["written"] = True
            except Exception as exc:  # noqa: BLE001
                # The shipment part is done and must not be redone every 15 minutes
                # (each retry is an edit -> relock on the order). Receipt the ASN,
                # report the order failure ONCE; the BOL's RTS line is blank until a
                # person keys it on the order.
                row["order_fields"]["error"] = str(exc)
                row["error"] = f"PRO/RTS/carrier written; order custom field NOT written: {exc}"
                counts["order_field_failed"] += 1
        tracking.record_finale_shipment(row["asn_id"], row["po_number"], sid, row["pro"], row["rts"], "prefilled")
        row["status"] = "prefilled"; counts["prefilled"] += 1

    out = {"mode": "live" if live else "dry", "results": results,
           "summary": {"candidates": len(asns), **counts}}
    if blocked:
        out["blocked"] = blocked
    return out
