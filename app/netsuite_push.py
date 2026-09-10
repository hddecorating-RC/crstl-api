"""
Shared engine for pushing Crstl invoices into NetSuite as INVOICE records via the
TBA REST connector. One code path, used by both the CLI (tools/push_invoices_to_
netsuite.py) and the web app (POST /api/netsuite), so a dry run and a live send
are always built the same way.

Pipeline per invoice:
    transform_invoice(...)      app.netsuite      -- business mapping (2 lines)
    build_invoice_payload(...)  app.netsuite_payload -- REST body, internal-id refs
    NetSuiteClient.upsert_invoice(...)               -- TBA transport (PUT eid:)

Safety:
  * dry run (live=False) builds and reconciles but sends nothing.
  * a live send is REFUSED while any item/tax id is unresolved in config
    (unresolved_ids); the caller gets the list back and no write happens.
  * a successful live upsert records a per-invoice "netsuite" event
    (app.tracking) so the dashboard's netsuite_at reflects it.

Results are plain JSON-serialisable dicts so the API can return them directly.
"""
from __future__ import annotations

from app.netsuite import transform_invoice
from app.netsuite_payload import build_invoice_payload, load_refs, unresolved_ids


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
    # Enforce eligibility HERE so no caller (esp. the CLI) can push ineligible
    # invoices. only/limit then apply to the eligible set.
    invoices = _select(eligible_for_push(invoices), only, limit)

    results: list[dict] = []
    prepared: list[tuple[dict, dict]] = []   # (result-row, payload) for rows to send
    unresolved: list[str] = []
    skipped_no_map = 0

    for inv in invoices:
        tid = str(inv.get("transaction_id", "?"))
        store, province = inv.get("store"), inv.get("province")
        lines = transform_invoice(inv, province, store)
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
        for tag in unresolved_ids(lines, refs):
            if tag not in unresolved:
                unresolved.append(tag)
        results.append(row)
        prepared.append((row, build_invoice_payload(lines, refs)))

    mode = "live" if live else "dry"
    sent = failed = skipped_modified = 0

    if live:
        if unresolved:
            # Refuse the whole batch; nothing is written.
            return {"mode": mode, "unresolved": unresolved, "results": results,
                    "summary": {"built": len(prepared), "sent": 0, "failed": 0,
                                "skipped_no_map": skipped_no_map, "skipped_modified": 0},
                    "blocked": "unresolved ids"}
        from app.netsuite_client import NetSuiteClient, NetSuiteUnavailable, NetSuiteModifiedOnServer
        from app import tracking
        if client is None:
            if not NetSuiteClient.configured():
                raise NetSuiteUnavailable("NETSUITE_* credentials not set")
            client = NetSuiteClient()
            client.test_connection()

        pushed_ids: list[str] = []
        for row, payload in prepared:
            eid = payload.get("externalId")
            try:
                # Optimistic lock: pass the lastModifiedDate we recorded when we
                # last wrote this record; the client aborts if NetSuite's copy has
                # changed since (someone edited our invoice) rather than overwrite.
                guard = tracking.get_netsuite_last_modified(eid) if eid else None
                result = client.upsert_invoice(payload, guard_last_modified=guard)
                row["status"] = "sent"
                row["location"] = result.get("location") or result
                # "created" vs "updated" -- proves a re-push UPDATED our own record
                # and never created a duplicate or touched another source.
                row["action"] = result.get("action") if isinstance(result, dict) else None
                if isinstance(result, dict) and eid:
                    # Store the new lastModifiedDate as the next push's guard.
                    tracking.record_netsuite_push(eid, result.get("netsuite_id"), result.get("last_modified"))
                sent += 1
                pushed_ids.append(row["transaction_id"])
            except NetSuiteModifiedOnServer as exc:
                # OMIS's "Record modified on server!" -- do NOT overwrite; flag it.
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
                    "skipped_no_map": skipped_no_map, "skipped_modified": skipped_modified},
    }
