"""
Shared engine for pushing Crstl invoices into NetSuite via the TBA REST connector.
The record type is config-driven (config/netsuite_customers.json: record_type) --
"salesOrder" (accounting's current preference; their team converts SO -> invoice)
or "invoice". One code path, used by both the CLI (tools/push_invoices_to_
netsuite.py) and the web app (POST /api/netsuite), so a dry run and a live send
are always built the same way.

Pipeline per invoice:
    transform_invoice(...)      app.netsuite      -- business mapping (2 lines)
    build_payload(...)          app.netsuite_payload -- REST body for record_type
    NetSuiteClient.upsert(...)                        -- TBA transport (PUT eid:)

Safety:
  * dry run (live=False) builds and reconciles but sends nothing.
  * a live send is REFUSED while any item/tax id is unresolved in config
    (unresolved_ids); the caller gets the list back and no write happens.
  * a successful live upsert records a per-invoice "netsuite" event
    (app.tracking) so the dashboard's netsuite_at reflects it.

Results are plain JSON-serialisable dicts so the API can return them directly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.netsuite import transform_invoice, amount_flag
from app.netsuite_payload import build_payload, load_refs, unresolved_ids


def _reconcile(inv: dict, lines: list[dict]) -> dict:
    """Per-invoice money view: gross -> discount -> net -> tax -> our total, and
    the delta vs the total on the 810 we sent Home Depot. The REST body drops
    per-line tax (NetSuite recomputes), so read it from the transform lines."""
    gross = lines[0]["amount"]
    discount = round(sum(l["amount"] for l in lines[1:]), 2)
    tax = round(sum(l["tax_amount"] for l in lines), 2)
    total = round(gross + discount + tax, 2)
    hd_total = inv.get("total_amount")
    return {
        "gross": gross,
        "discount": discount,
        "net": round(gross + discount, 2),
        "tax": tax,
        "total": total,
        "hd_total": hd_total,
        "delta": None if hd_total is None else round(total - hd_total, 2),
    }


def select_latest_accepted(invoices: list[dict]) -> list[dict]:
    """One invoice per logical invoice (source_document_id), Accepted only.

    CRSTL sends many rows per invoice -- drafts plus resubmissions -- each with
    its own transaction_id but a shared source_document_id. We push only rows HD
    has Accepted, and when several Accepted versions exist we keep the LATEST (by
    invoice_date, then transaction_id, whose ObjectId embeds creation time) so a
    stale version can't win the upsert. Rows that are not Accepted, or carry no
    source_document_id, are dropped. Order of the returned list is not defined.
    """
    best: dict[str, dict] = {}
    for inv in invoices:
        if (inv.get("status") or "") != "Accepted":
            continue
        sid = str(inv.get("source_document_id") or "").strip()
        if not sid:
            continue
        key = (str(inv.get("invoice_date") or ""), str(inv.get("transaction_id") or ""))
        cur = best.get(sid)
        if cur is None or key > (str(cur.get("invoice_date") or ""), str(cur.get("transaction_id") or "")):
            best[sid] = inv
    return list(best.values())


def eligible_for_push(invoices: list[dict]) -> list[dict]:
    """The SINGLE definition of what the connector may push: Accepted, one (latest)
    version per logical invoice (source_document_id), non-zero gross. Enforced
    inside push_invoices() so EVERY caller -- the web button, the dry-run preview,
    the 5am scheduled job AND the CLI -- gets it; no path can push a Draft, a stale
    resubmission, or a zero-value row by going around it."""
    return [i for i in select_latest_accepted(invoices) if (i.get("subtotal") or 0) > 0]


def select_for_automation(candidates: list[dict], unpushed_ids, *,
                          created_after: str | None = None,
                          created_within_days: int | None = None,
                          max_per_run: int | None = None,
                          now: datetime | None = None) -> tuple[list[dict], str | None]:
    """AUTOMATION-ONLY selection for the scheduled push job, layered on top of
    eligible_for_push. NOT used by push_invoices, so manual pushes (dashboard/CLI)
    are never limited by this -- a human can deliberately push anything, including
    old or pre-cutoff invoices.

    The date guards key on `created_at` -- WHEN CRSTL created the record -- NOT
    invoice_date. invoice_date can be back-dated or garbage (we have live rows
    stamped invoice_date 2008 that CRSTL actually created in 2025), whereas
    created_at is a reliable server timestamp present on every row. Comparison is
    lexical on the YYYY-MM-DD prefix; a row with no created_at is treated as before
    the floor (excluded).

    Applies three guards and returns (to_push, blocked_reason):
      * created_after -- an absolute FLOOR on created_at: nothing created before
        this date is ever auto-pushed (the 2026-09-11 149-incident backstop).
      * created_within_days -- a ROLLING recency window: only auto-push invoices
        created within the last N days. This is the self-scaling guard -- it tracks
        real inflow, so it needs no retuning as daily volume grows, and a scope slip
        that dredges up the old backlog (created weeks/months ago) falls outside the
        window and is never touched. The effective lower bound is the LATER of
        created_after and (now - N days), so the floor is never relaxed below it.
      * max_per_run -- a hard per-run CAP: if the resulting set exceeds it, REFUSE
        the whole run (return [] and a reason) rather than risk a blast. A day that
        large should be reviewed and run manually. `blocked_reason` is None when the
        run may proceed.
    """
    now = now or datetime.now(timezone.utc)
    floor = created_after or ""
    if created_within_days is not None:
        window_start = (now - timedelta(days=created_within_days)).strftime("%Y-%m-%d")
        floor = max(floor, window_start)   # the tighter (later) bound wins
    if floor:
        candidates = [i for i in candidates
                      if str(i.get("created_at") or "")[:10] >= floor]
    unpushed = {str(x) for x in unpushed_ids}
    to_push = [i for i in candidates if str(i.get("transaction_id")) in unpushed]
    if max_per_run is not None and len(to_push) > max_per_run:
        return [], f"{len(to_push)} to push exceeds max_per_run {max_per_run}"
    return to_push, None


def _select(invoices: list[dict], only: list[str] | None, limit: int | None) -> list[dict]:
    if only:
        wanted = {str(x) for x in only}
        invoices = [i for i in invoices if str(i.get("transaction_id")) in wanted]
    # `is not None`, not truthiness: limit=0 must mean "cap at 0", never "no cap".
    if limit is not None:
        invoices = invoices[:limit]
    return invoices


def push_invoices(
    invoices: list[dict],
    *,
    live: bool = False,
    only: list[str] | None = None,
    limit: int | None = None,
    refs: dict | None = None,
    client=None,
    confirm_existing: bool = False,
) -> dict:
    """Build (and, when live, send) NetSuite invoices for these Crstl invoices.

    Returns {mode, unresolved, results, summary}:
      * results: one dict per invoice with transaction_id, channel, where,
        status (built|sent|failed|skipped_no_map), the reconciliation numbers,
        and location (live) or error.
      * summary: counts of built/sent/failed/skipped_no_map.
    A live send is refused (nothing written) if `unresolved` is non-empty.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    refs = refs or load_refs()
    # Which NetSuite record this run creates ("invoice" or "salesOrder"), from
    # config. Threaded to the payload builder, the connection check and the upsert
    # so all three agree -- a dry run and a live send build the same record type.
    record_type = refs.get("record_type") or "invoice"
    # Enforce eligibility HERE so no caller (esp. the CLI) can push ineligible
    # invoices. only/limit then apply to the eligible set.
    invoices = _select(eligible_for_push(invoices), only, limit)

    results: list[dict] = []
    prepared: list[tuple[dict, dict]] = []   # (result-row, payload) for rows to send
    unresolved: list[str] = []
    skipped_no_map = 0
    skipped_invalid = 0
    skipped_no_baseline = 0
    skipped_exists = 0        # already in NetSuite; needs explicit confirm to update
    skipped_conflict = 0      # externalId held by a different transaction type (invoice)

    for inv in invoices:
        tid = str(inv.get("transaction_id", "?"))
        store, province = inv.get("store"), inv.get("province")
        try:
            lines = transform_invoice(inv, province, store)
        except ValueError as exc:
            # Malformed invoice (e.g. an unsafe source_document_id, H2): skip and
            # flag. One bad row must never abort the whole batch (L2), and an
            # unsafe id must never reach the NetSuite URL.
            skipped_invalid += 1
            results.append({"transaction_id": tid, "invoice_number": inv.get("invoice_number"),
                            "channel": None, "where": None, "status": "skipped_invalid",
                            "error": str(exc)})
            continue
        if not lines:
            skipped_no_map += 1
            results.append({"transaction_id": tid, "invoice_number": inv.get("invoice_number"),
                            "channel": None, "where": None, "status": "skipped_no_map"})
            continue
        row = {
            "transaction_id": tid,
            "invoice_number": inv.get("invoice_number"),
            "channel": "dsd" if store else "dropship",
            "where": store or province,
            "customer_id": lines[0].get("customer_id"),
            "customer_name": lines[0].get("customer_name"),
            "status": "built",
            **_reconcile(inv, lines),
        }
        row["amount_flag"] = amount_flag(row.get("gross"), row["channel"])
        # Reconcile guard: our total should equal CRSTL's 810 total (hd_total).
        # Flag only STRUCTURAL mismatches (> 1 cent) -- a 1c delta is expected on
        # compound-tax provinces (QC GST+QST): CRSTL and NetSuite's tax
        # GROUP round each component separately, while our single-rate reconcile
        # rounds the combined rate. The real errors (e.g. a missing PST) are dollars,
        # never a cent, so a 1c tolerance keeps the guard sharp without false alarms.
        d = row.get("delta")
        row["reconcile_flag"] = None if (d is None or abs(d) <= 0.01) else f"total off CRSTL 810 by {d:+.2f}"
        for tag in unresolved_ids(lines, refs):
            if tag not in unresolved:
                unresolved.append(tag)
        results.append(row)
        prepared.append((row, build_payload(lines, refs, record_type)))

    mode = "live" if live else "dry"
    sent = failed = skipped_modified = 0

    if live:
        if unresolved:
            # Refuse the whole batch; nothing is written.
            return {"mode": mode, "unresolved": unresolved, "results": results,
                    "summary": {"built": len(prepared), "sent": 0, "failed": 0,
                                "skipped_no_map": skipped_no_map, "skipped_modified": 0,
                                "skipped_invalid": skipped_invalid, "skipped_no_baseline": skipped_no_baseline,
                                "skipped_exists": 0, "skipped_conflict": 0},
                    "blocked": "unresolved ids"}
        from app.netsuite_client import (NetSuiteClient, NetSuiteUnavailable, NetSuiteModifiedOnServer,
                                          NetSuiteNoBaseline, NetSuiteExternalIdConflict)
        from app import tracking
        if client is None:
            if not NetSuiteClient.configured():
                raise NetSuiteUnavailable("NETSUITE_* credentials not set")
            client = NetSuiteClient()
            client.test_connection(record_type)

        pushed_ids: list[str] = []
        for row, payload in prepared:
            eid = payload.get("externalId")
            # VALIDATION: never overwrite a record already booked in NetSuite without
            # an explicit confirm. Read-only existence check first; a cross-type eid
            # collision (an invoice already holds our eid) is surfaced clearly.
            try:
                existing = client.get_by_external_id(record_type, eid) if eid else None
            except NetSuiteExternalIdConflict as exc:
                row["status"] = "skipped_conflict"; row["error"] = str(exc)
                skipped_conflict += 1
                continue
            except Exception as exc:
                row["status"] = "failed"; row["error"] = str(exc)
                failed += 1
                continue
            if existing is not None and not confirm_existing:
                # Already in NetSuite. Refuse to touch it unless the caller confirms --
                # a booked record was put there deliberately (by us, or accounting).
                row["status"] = "skipped_exists"
                row["action"] = "exists"
                row["error"] = (f"already in NetSuite as {record_type} "
                                f"{existing.get('tranId') or existing.get('id')} — confirm to update")
                skipped_exists += 1
                continue
            try:
                # New -> create. Existing + confirmed -> update, guarding against a
                # concurrent edit between our read and the write (fresh lastModified).
                guard = existing.get("lastModifiedDate") if existing is not None else None
                result = client.upsert(payload, record_type, guard_last_modified=guard)
                row["status"] = "sent"
                row["location"] = result.get("location") or result
                # "created" vs "updated" -- proves a confirmed re-push UPDATED our own
                # record and never created a duplicate or touched another source.
                row["action"] = result.get("action") if isinstance(result, dict) else None
                if isinstance(result, dict) and eid:
                    tracking.record_netsuite_push(eid, result.get("netsuite_id"), result.get("last_modified"))
                sent += 1
                pushed_ids.append(row["transaction_id"])
            except NetSuiteNoBaseline as exc:
                row["status"] = "skipped_no_baseline"
                row["error"] = str(exc)
                skipped_no_baseline += 1
            except NetSuiteModifiedOnServer as exc:
                # Changed in NetSuite between our read and the write -- do NOT overwrite.
                row["status"] = "skipped_modified"
                row["error"] = str(exc)
                skipped_modified += 1
            except Exception as exc:  # one bad invoice must not stop the batch
                row["status"] = "failed"
                row["error"] = str(exc)
                failed += 1

        if pushed_ids:
            # Best-effort per-invoice log; never let a tracking hiccup fail a send.
            try:
                tracking.record_events(pushed_ids, "netsuite")
            except Exception as exc:  # noqa: BLE001
                print(f"WARNING: netsuite push succeeded but tracking failed: {exc}")

    return {
        "mode": mode,
        "unresolved": unresolved,
        "results": results,
        "summary": {"built": len(prepared), "sent": sent, "failed": failed,
                    "skipped_no_map": skipped_no_map, "skipped_modified": skipped_modified,
                    "skipped_invalid": skipped_invalid, "skipped_no_baseline": skipped_no_baseline,
                    "skipped_exists": skipped_exists, "skipped_conflict": skipped_conflict},
    }
