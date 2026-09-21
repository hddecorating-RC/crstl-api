"""Finale + warehouse automation: Finale invoicing (EDI and non-EDI), DSD pickup
numbers, ShipStation DSD close, dropship pre-fill, and the Finale reconciliation the
digest reads. The 15-min poll (_run_finale_push_job) is the Finale worker's job."""
import contextlib
import fcntl
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import tracking
from app.netsuite_push import eligible_for_push, select_for_automation
from app.finale_invoice import push_finale_invoices
from app.shipstation import ShipStationClient, push_shipstation_close
from app.dropship import push_dropship_prefill
from app.finale_nonedi import push_nonedi_invoices
from app.finale_dsd import push_dsd_prefill, select_dsd_asns
from app.netsuite_payload import load_refs
from app.automation import AUTO_DSD_SETTING, AUTO_FINALE_SETTING
from app.crstl_cache import _cache, _cache_lock
from app import automation, crstl_cache


_finale_push_lock = threading.Lock()
_finale_push_state: dict = {"last_run": None, "mode": None, "summary": None,
                            "results": None, "blocked": None, "error": None, "running": False,
                            "nonedi": None}

# ONE run at a time across EVERY Finale-writing entry point -- the 15-min poll, the
# NetSuite ride-along, and the three manual endpoints. Each runner takes this lock
# itself, so no caller can forget it; the poll holds it across its three passes.
# Re-entrant so a pass inside the poll re-acquires on the same thread. Non-blocking:
# a second run does not queue up behind the first (it would only redo the same
# reads), it is refused with FinaleBusy and the poll comes round in 15 minutes.
# Why: preflight (receipt + order invoices) and the create POST are not atomic, and
# Finale's collection POST always creates -- two overlapping runs could post two
# invoices on one order.
#
# Two halves since 2026-09-21, when the poll moved into its own process (the Finale
# worker) while the manual endpoints and the NetSuite ride-along stayed in the web app:
# the RLock covers threads in THIS process, and an flock on a file beside tracking.db
# covers every process on the box. The kernel drops an flock when its process dies, so
# a crash can never leave the lock held.
_finale_run_lock = threading.RLock()
_finale_run_holder: Optional[str] = None
_finale_run_fd: Optional[int] = None


class FinaleBusy(RuntimeError):
    """Another Finale run holds the lock; nothing was done."""


def _run_lock_path() -> str:
    folder = os.path.dirname(os.path.abspath(tracking._db_path()))
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "finale-run.lock")


def _run_lock_holder() -> Optional[str]:
    """Who holds the Finale run lock in ANY process ("poll, pid 123"), or None. Read
    from the note the holder leaves in the lock file; a note left by a process that
    has since died is ignored."""
    try:
        with open(_run_lock_path()) as f:
            note = f.read().strip()
    except OSError:
        return None
    label, _, pid = note.rpartition(" pid ")
    if not label:
        return None
    try:
        os.kill(int(pid), 0)
    except (ValueError, ProcessLookupError):
        return None
    except PermissionError:
        pass                                  # alive, owned by another user
    return f"{label}, pid {pid}"


@contextlib.contextmanager
def _finale_run(label: str):
    global _finale_run_holder, _finale_run_fd
    if not _finale_run_lock.acquire(blocking=False):
        raise FinaleBusy(f"another Finale run is in progress ({_finale_run_holder or 'unknown'})")
    outer = _finale_run_holder is None
    if outer:
        try:
            fd = os.open(_run_lock_path(), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            _finale_run_lock.release()
            raise
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            _finale_run_lock.release()
            raise FinaleBusy(f"another Finale run is in progress ({_run_lock_holder() or 'another process'})")
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{label} pid {os.getpid()}".encode(), 0)
        _finale_run_fd = fd
        _finale_run_holder = label
        with _finale_push_lock:
            _finale_push_state["running"] = True
    try:
        yield
    finally:
        if outer:
            _finale_run_holder = None
            with _finale_push_lock:
                _finale_push_state["running"] = False
            fd, _finale_run_fd = _finale_run_fd, None
            try:
                os.ftruncate(fd, 0)
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        _finale_run_lock.release()


# The last run of each pass, for the dashboard (GET /api/finale-push/latest). Kept in
# memory AND in tracking.db: the web app serves the dashboard, but the poll runs in
# the Finale worker and alerts in order-watch, so the web app reads what they wrote.
# "edi" is the top-level Finale-invoicing fields; the others are one pass each.
_STATE_SECTIONS = ("nonedi", "dsd", "shipstation", "dropship", "alerts")
_EDI_STATE_KEYS = ("last_run", "mode", "summary", "results", "blocked", "error")


def _save_state(section: str, data: dict) -> None:
    key = f"finale_state:{section}"
    with _finale_push_lock:
        if section == "edi":
            _finale_push_state.update(data)
        else:
            _finale_push_state[section] = data
    # "edi" merges (a ride-along error updates just `error`), into what is STORED --
    # this process's memory may be older than a run the other process made.
    tracking.set_json(key, {**(tracking.get_json(key) or {}), **data} if section == "edi" else data)


def finale_state() -> dict:
    """What the dashboard shows: each pass's last run, whichever process made it, and
    whether a Finale run is in progress in any process."""
    with _finale_push_lock:
        out = dict(_finale_push_state)
    stored = tracking.get_json("finale_state:edi") or {}
    out.update({k: stored[k] for k in _EDI_STATE_KEYS if k in stored})
    for section in _STATE_SECTIONS:
        v = tracking.get_json(f"finale_state:{section}")
        if v is not None:
            out[section] = v
    out["running"] = bool(out.get("running")) or _run_lock_holder() is not None
    return out


def _finale_config() -> dict:
    return load_refs().get("finale") or {}


def _finale_enabled() -> bool:
    return bool(_finale_config().get("enabled")) and automation._job_enabled(AUTO_FINALE_SETTING, default="false")


def _finale_listings(client) -> tuple[Optional[set], Optional[dict]]:
    """For the poll's read shortcut: the POs with a shipped/delivered shipment, and
    every sale order's status -- two listings (collection requests; Finale allows 300
    an hour). (None, None) if either fails: the pass then reads every order, as before."""
    from app.dropship import po_of
    from app.finale import MOVED
    try:
        shipped = {po_of(s) for s in client.list_shipments() if str(s.get("statusId") or "") in MOVED}
        status = {str(o.get("orderId")): str(o.get("statusId") or "") for o in client.list_sale_orders()}
        return shipped, status
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: Finale invoicing: listings failed, reading every order: {exc}")
        return None, None


def _run_finale_push(live: bool, ids: Optional[list[str]], limit: Optional[int],
                     max_per_run: Optional[int] = None, prefilter: bool = False) -> dict:
    """Create (live) or preview (dry) Finale invoices for cached invoices via the
    shared engine, and record the run. The engine enforces eligibility, the exact-
    cents build, the reconcile + qty gates (posted vs draft), idempotency and, for
    the automated callers that pass it, the blast cap on invoices about to be
    created. Manual runs (the endpoint) are uncapped, like manual NetSuite pushes.
    `prefilter` (the 15-min poll only): skip the reads for orders the listings show
    still open with nothing shipped -- see push_finale_invoices."""
    from app.finale import FinaleClient
    with _cache_lock:
        invoices = list(_cache["invoices"])
        po_map = dict(_cache["po_provinces"])
    with _finale_run("edi"):
        client = shipped_pos = order_status = None
        if prefilter and FinaleClient.configured():
            client = FinaleClient()
            shipped_pos, order_status = _finale_listings(client)
        result = push_finale_invoices(invoices, po_map, live=live, only=ids, limit=limit, max_per_run=max_per_run,
                                      client=client, shipped_pos=shipped_pos, order_status=order_status)
    _save_state("edi", {
        "last_run": datetime.now(timezone.utc).isoformat(), "mode": result["mode"],
        "summary": result["summary"], "results": result["results"],
        "blocked": result.get("blocked"), "error": None,
    })
    s = result["summary"]
    tracking.record_job_run("finale_push", "blocked" if result.get("blocked") else
                            ("ok" if s["failed"] == 0 else "partial"),
                            f"{s['posted']} posted, {s['draft']} draft, {s['failed']} failed "
                            f"[{'live' if live else 'dry'}]" + (f" -- {result['blocked']}" if result.get("blocked") else ""))
    return result


def _finale_floor() -> Optional[str]:
    """Finale's OWN positive floor ("orders moving forward"), falling back to the
    shared automation one. Every automated Finale write is scoped by it."""
    auto = load_refs().get("automation") or {}
    return str(_finale_config().get("go_live_after") or auto.get("go_live_after") or "") or None


def _finale_scope(ids: list[str]) -> tuple[list[str], list[str]]:
    """Apply the automation date guards to these transaction ids: (in_scope,
    out_of_scope). Same floor / rolling window as the 15-min poll, keyed on the
    cached invoice's created_at -- an id the cache does not know is out of scope.
    The blast cap is the engine's (on invoices about to be created)."""
    auto = load_refs().get("automation") or {}
    wanted = {str(i) for i in ids}
    with _cache_lock:
        cands = [i for i in _cache["invoices"] if str(i.get("transaction_id")) in wanted]
    chosen, _ = select_for_automation(cands, wanted, created_after=_finale_floor(),
                                      created_within_days=auto.get("created_within_days"), max_per_run=None)
    kept = [str(i["transaction_id"]) for i in chosen]
    return kept, [i for i in ids if str(i) not in set(kept)]


def _run_finale_push_safe(sent_ids: list[str]) -> None:
    """Invoice in Finale the invoices a live NetSuite push just sent. Best-effort:
    never fails the NetSuite push. Scoped exactly like the poll -- the Finale floor,
    the rolling window and max_per_run -- so a manual (unlimited) NetSuite push of an
    old invoice never reaches into orders the warehouse invoiced by hand."""
    ids, dropped = _finale_scope(sent_ids)
    if not ids:
        tracking.record_job_run("finale_push", "skipped",
                                f"{len(dropped)} sent to NetSuite, none inside the Finale floor/window")
        return
    if dropped:
        tracking.record_job_run("finale_push", "skipped",
                                f"{len(dropped)} sent to NetSuite left alone (before the Finale floor/window)")
    try:
        _run_finale_push(True, ids, None, max_per_run=_finale_config().get("max_per_run"))
    except FinaleBusy as exc:
        tracking.record_job_run("finale_push", "skipped", f"{exc} -- the 15-min poll will invoice them")
    except Exception as exc:
        _save_state("edi", {"error": str(exc)})
        print(f"WARNING: Finale invoicing failed after NetSuite push: {exc}")
        tracking.record_job_run("finale_push", "error", str(exc)[:200])


def _finale_edi_pass() -> None:
    """The EDI half of the poll: incremental 810 refresh, then invoice the Accepted
    810s not yet invoiced, under the automation date guards; the engine applies
    finale.max_per_run to the invoices it is about to create (a pending, unshipped
    810 is waiting, not writing). Early returns here only end THIS pass -- the
    non-EDI pass still runs after it."""
    crstl_cache._refresh_new_accepted()
    auto = load_refs().get("automation") or {}
    with _cache_lock:
        invoices = list(_cache["invoices"])
    candidates = eligible_for_push(invoices)
    todo = tracking.get_unfinaled_ids([str(i["transaction_id"]) for i in candidates])
    to_push, _ = select_for_automation(candidates, todo,
                                       created_after=_finale_floor(),
                                       created_within_days=auto.get("created_within_days"),
                                       max_per_run=None)
    if not to_push:
        tracking.record_job_run("finale_push", "ok", "nothing new to invoice"); return
    _run_finale_push(True, [str(i["transaction_id"]) for i in to_push], None,
                     max_per_run=_finale_config().get("max_per_run"), prefilter=True)


def _run_finale_push_job() -> None:
    """Every 15 minutes, two passes -- BOTH gated by the same two switches (config
    finale.enabled AND the dashboard toggle): (1) EDI: Accepted 810s not yet invoiced,
    shipped in Finale; (2) non-EDI: shipped Finale sale orders that are not Crstl POs
    (HD Supply, Special Orders, future OMIS...); (3) when the DSD toggle is on, DSD:
    PRO/RTS from newly Accepted 856s onto their open Finale shipments. One pass failing
    never stops the others. An order not yet shipped in Finale is skipped by the engine
    and retried next run."""
    if not _finale_enabled():
        tracking.record_job_run("finale_push", "skipped", "disabled"); return
    try:
        with _finale_run("poll"):
            _finale_poll_passes()
    except FinaleBusy as exc:
        tracking.record_job_run("finale_push", "skipped", f"{exc} -- next poll in 15 min")


def _finale_poll_passes() -> None:
    try:
        _finale_edi_pass()
    except Exception as exc:
        print(f"WARNING: Finale poll failed: {exc}")
        tracking.record_job_run("finale_push", "error", str(exc)[:200])
    try:
        _run_nonedi_push(True, None, None)
    except Exception as exc:
        print(f"WARNING: non-EDI Finale poll failed: {exc}")
        tracking.record_job_run("finale_nonedi", "error", str(exc)[:200])
    if _dsd_prefill_enabled():
        try:
            _run_dsd_prefill(True, None, None)
        except Exception as exc:
            print(f"WARNING: DSD prefill poll failed: {exc}")
            tracking.record_job_run("finale_dsd", "error", str(exc)[:200])
    if _shipstation_config().get("enabled"):
        try:
            _run_shipstation_close(True, None, None)
        except Exception as exc:
            print(f"WARNING: ShipStation close poll failed: {exc}")
            tracking.record_job_run("shipstation_close", "error", str(exc)[:200])
    if _dropship_config().get("enabled"):
        try:
            _run_dropship_prefill(True, None, None)
        except Exception as exc:
            print(f"WARNING: dropship pre-fill poll failed: {exc}")
            tracking.record_job_run("dropship_prefill", "error", str(exc)[:200])


def _dsd_prefill_enabled() -> bool:
    """The dashboard toggle (Automation panel, default OFF). No config switch: the
    pass already sits inside the Finale poll, which has its own two gates."""
    return automation._job_enabled(AUTO_DSD_SETTING, default="false")


def _run_dsd_prefill(live: bool, ids: Optional[list[str]], limit: Optional[int]) -> dict:
    """Copy PRO/RTS from Accepted DSD 856s onto their open Finale shipments (live) or
    preview it (dry). `ids` names ASN ids; a manual run may name any Accepted ASN, the
    automated pass (ids=None) applies the shared guards: go_live_after floor, the
    created_within_days window, max_per_run, and one receipt per ASN."""
    from app.finale import FinaleClient, FinaleUnavailable
    if not FinaleClient.configured():
        raise FinaleUnavailable("FINALE_* credentials not set")
    crstl = crstl_cache._get_client()
    fin, auto = _finale_config(), (load_refs().get("automation") or {})
    automated = ids is None
    if automated:
        states = crstl.list_transaction_states(transaction_type="856", created_after=crstl_cache._poll_window())
        todo = tracking.get_unprefilled_asn_ids(list(states))
        ids = select_dsd_asns(states, set(states) - set(todo),
                              created_after=_finale_floor(),
                              created_within_days=auto.get("created_within_days"))
    # A manual run may name any ASN, so only the automated pass narrows the listing.
    asns = crstl.fetch_asn_refs(ids, created_after=crstl_cache._poll_window() if automated else None) if ids else []
    with _finale_run("dsd"):
        result = push_dsd_prefill(asns, live=live, only=None, limit=limit, client=FinaleClient(),
                                  max_per_run=fin.get("max_per_run") if automated else None)
    blocked = result.get("blocked")
    _save_state("dsd", {"last_run": datetime.now(timezone.utc).isoformat(), **result})
    s = result["summary"]
    tracking.record_job_run("finale_dsd", "blocked" if blocked else ("ok" if not s.get("failed") else "partial"),
                            (f"{blocked} -- refusing; run manually" if blocked else
                             f"{s['candidates']} ASNs: {s.get('prefilled', 0)} prefilled, {s.get('would_prefill', 0)} would, "
                             f"{s.get('skipped_no_shipment', 0) + s.get('skipped_no_order', 0)} waiting, "
                             f"{s.get('skipped_shipped', 0)} already shipped, {s.get('failed', 0)} failed"
                             + (f", {s['order_field_failed']} order field NOT written" if s.get("order_field_failed") else ""))
                            + f" [{'live' if live else 'dry'}]")
    return result


def _shipstation_config() -> dict:
    return load_refs().get("shipstation") or {}


def _run_shipstation_close(live: bool, ids: Optional[list[str]], limit: Optional[int]) -> dict:
    """Mark the DSD store's open ShipStation orders shipped once Finale shows them
    shipped (live) or preview it (dry). `ids` names ShipStation order NUMBERS (HD
    POs); a manual run may name any open order, the automated pass (ids=None)
    applies the floor and the cap. Reads ShipStation + Finale; the only write is
    ShipStation's Mark as Shipped."""
    from app.finale import FinaleClient, FinaleUnavailable
    if not ShipStationClient.configured():
        raise FinaleUnavailable("SHIPSTATION_V1_KEY / SHIPSTATION_V1_SECRET not set")
    if not FinaleClient.configured():
        raise FinaleUnavailable("FINALE_* credentials not set")
    cfg = _shipstation_config()
    automated = ids is None
    client = ShipStationClient()
    orders = client.list_open_orders(int(cfg.get("dsd_store_id") or 0))
    result = push_shipstation_close(orders, live=live, only=ids, limit=limit, client=client, finale=FinaleClient(),
                                    created_after=(str(cfg.get("go_live_after") or "") or None) if automated else None,
                                    carrier_code=str(cfg.get("carrier_code") or "other"),
                                    max_per_run=cfg.get("max_per_run") if automated else None)
    blocked = result.get("blocked")
    _save_state("shipstation", {"last_run": datetime.now(timezone.utc).isoformat(), **result})
    c = result["summary"]
    tracking.record_job_run("shipstation_close", "blocked" if blocked else ("ok" if not c.get("failed") else "partial"),
                            (f"{blocked} -- refusing; run manually" if blocked else
                             f"{c['candidates']} open: {c.get('close_done', 0)} closed, {c.get('would_close', 0)} would, "
                             f"{c.get('skipped_not_shipped', 0) + c.get('skipped_no_finale', 0)} waiting, "
                             f"{c.get('skipped_floor', 0)} pre-floor, {c.get('skipped_cancelled', 0)} cancelled in Finale, "
                             f"{c.get('failed', 0)} failed") + f" [{'live' if live else 'dry'}]")
    return result


def _dropship_config() -> dict:
    return load_refs().get("dropship_prefill") or {}


def _run_dropship_prefill(live: bool, ids: Optional[list[str]], limit: Optional[int]) -> dict:
    """Write carrier + tracking onto the packed Finale shipment of each dropship label
    (live) or preview it (dry). `ids` names PO numbers; the automated pass (ids=None)
    applies the label-date floor and the cap. Listings for shipments, sale orders and
    carriers; a shipment is read only if it has not been filled in yet, an order only
    if it has to be reopened (see app.dropship). Live runs remember what they found."""
    from app.finale import FinaleClient, FinaleUnavailable, wanted_carrier
    if not ShipStationClient.configured():
        raise FinaleUnavailable("SHIPSTATION_V1_KEY / SHIPSTATION_V1_SECRET not set")
    if not FinaleClient.configured():
        raise FinaleUnavailable("FINALE_* credentials not set")
    cfg, automated = _dropship_config(), ids is None
    client = FinaleClient()
    since = (datetime.now(timezone.utc) - timedelta(days=int(cfg.get("lookback_days") or 3))).strftime("%Y-%m-%d")
    labels = ShipStationClient().list_shipments(int(cfg.get("store_id") or 0), since)
    carrier = wanted_carrier(_finale_config(), "dropship", client.carrier_index())
    try:
        order_status = {str(o.get("orderId")): str(o.get("statusId") or "") for o in client.list_sale_orders()}
    except Exception as exc:  # noqa: BLE001 -- without it, read each order as before
        print(f"WARNING: dropship pre-fill: sale-order listing failed, reading orders one by one: {exc}")
        order_status = None
    with _finale_run("dropship"):
        result = push_dropship_prefill(client.list_shipments(), labels, live=live, only=ids, limit=limit,
                                       client=client, carrier_url=carrier["url"] if carrier["enabled"] else None,
                                       created_after=(str(cfg.get("go_live_after") or "") or None) if automated else None,
                                       max_per_run=cfg.get("max_per_run") if automated else None,
                                       order_status=order_status, marks=tracking.get_dropship_marks())
    if live:
        tracking.record_dropship_marks([r["mark"] for r in result["results"] if r.get("mark")])
    result["carrier"] = {"wanted": carrier["name"], "enabled": carrier["enabled"], "note": carrier["reason"] or None}
    blocked = result.get("blocked")
    _save_state("dropship", {"last_run": datetime.now(timezone.utc).isoformat(), **result})
    c = result["summary"]
    tracking.record_job_run("dropship_prefill", "blocked" if blocked else ("ok" if not c.get("failed") else "partial"),
                            (f"{blocked} -- refusing; run manually" if blocked else
                             f"{c['candidates']} label(s): {c.get('prefilled', 0)} prefilled, {c.get('would_prefill', 0)} would, "
                             f"{c.get('skipped_equal', 0)} already set, {c.get('skipped_no_shipment', 0)} waiting, "
                             f"{c.get('skipped_shipped', 0)} shipped, {c.get('skipped_ambiguous', 0)} ambiguous, "
                             f"{c.get('failed', 0)} failed") + f" [{'live' if live else 'dry'}]")
    return result


def _run_nonedi_push(live: bool, ids: Optional[list[str]], limit: Optional[int]) -> dict:
    """Invoice (live) or preview (dry) shipped non-EDI sale orders in Finale. Reads the
    sale-order list + party provinces from Finale; the engine does the rest."""
    from app.finale import FinaleClient, FinaleUnavailable
    if not FinaleClient.configured():
        raise FinaleUnavailable("FINALE_* credentials not set")
    client = FinaleClient()
    fin = _finale_config()
    with _finale_run("nonedi"):
        result = push_nonedi_invoices(client.list_sale_orders(), crstl_cache._crstl_po_set(), live=live, only=ids, limit=limit,
                                      client=client, floor=str(fin.get("nonedi_go_live_after") or "") or None,
                                      max_per_run=fin.get("max_per_run") if ids is None else None,   # automation only
                                      auto_reopen=bool(fin.get("auto_reopen")))
    _save_state("nonedi", {"last_run": datetime.now(timezone.utc).isoformat(), **result})
    s = result["summary"]
    tracking.record_job_run("finale_nonedi", "blocked" if result.get("blocked") else ("ok" if s["failed"] == 0 else "partial"),
                            f"{s['candidates']} candidates: {s['posted']} posted, {s['draft']} draft, {s['failed']} failed, "
                            f"{s['skipped_not_shipped']} not shipped [{'live' if live else 'dry'}]"
                            + (f" -- {result['blocked']}" if result.get("blocked") else ""))
    return result


_EMPTY_RECON: dict = {"floor": None, "stale_days": None, "missing": [], "deltas": [], "drafts": [], "by_tx": {}, "rows": []}


def _finale_reconciliation(invoices: list[dict], stale_days) -> dict:
    """Does Finale match CRSTL and NetSuite? Rolling -- an SO stays listed every day
    until it clears -- but FLOORED at Finale's go-live date (on the 810's created_at,
    the same guard as the poll): orders from before it were invoiced another way and
    are known not to tie, so they never appear. Scope = eligible 810s on/after the
    floor that have a NetSuite SO. For each: the Finale receipt (ours, or 'external'
    for one someone else keyed), else a read-only dry run for WHY there is none.
      missing -- no invoice in Finale, with the live reason (not shipped / no order /
                 create failed / shipped and about to be invoiced); `stale` when the
                 810 was accepted over `stale_days` ago and still nothing shipped.
      deltas  -- an invoice whose total is off the 810 (whoever created it).
      drafts  -- invoices we hold un-posted (re-read first: one posted by hand since
                 is receipted as posted and drops off).
      by_tx   -- the Finale view per transaction (receipt or dry-run external)."""
    from app.finale import API_LOGIN, FinaleClient, approved_by, created_by, invoice_total
    floor = _finale_floor()
    if not floor:
        return dict(_EMPTY_RECON)
    scoped = [i for i in eligible_for_push(invoices) if str(i.get("created_at") or "")[:10] >= floor]
    ids = [str(i["transaction_id"]) for i in scoped]
    events = tracking.get_latest_events(ids)
    scoped = [i for i in scoped if events.get(str(i["transaction_id"]), {}).get("netsuite_at")]
    ids = [str(i["transaction_id"]) for i in scoped]
    receipts = tracking.get_finale_invoices(ids)
    configured = FinaleClient.configured()
    hd_total = {str(i["transaction_id"]): i.get("total_amount") for i in scoped}
    if configured:
        client = FinaleClient()
        # Re-read (bounded) the receipts that need it: a draft we hold, to see if it
        # was posted by hand since; and a receipt from before the reconciliation
        # columns existed (no total), to fill in who / total / delta once.
        needs = [(tx, rec) for tx, rec in receipts.items()
                 if rec.get("invoice_url") and (rec.get("status") == "draft" or rec.get("finale_total") is None)]
        for tx, rec in needs[:50]:          # bounded reads per digest, of the ones that need it
            draft = rec.get("status") == "draft"
            try:
                live = client.get_invoice(rec["invoice_url"])
            except Exception:  # noqa: BLE001 -- a read failure leaves the receipt as it was
                continue
            new = dict(rec)
            if draft and live.get("statusId") == "INVOICE_APPROVED":
                new["status"] = "posted"
                new["created_by"] = approved_by(live) or rec.get("created_by")
            if rec.get("finale_total") is None:
                new["finale_total"] = invoice_total(live)
                ht = hd_total.get(tx)
                new["delta"] = None if ht is None else round(new["finale_total"] - float(ht), 2)
                new["created_by"] = new.get("created_by") or created_by(live) or API_LOGIN
            if new != rec:
                tracking.record_finale_invoice(tx, rec.get("po_number"), rec.get("invoice_id"), rec.get("invoice_url"),
                                               rec.get("invoice_id_user"), new["status"], created_by=new.get("created_by"),
                                               finale_total=new.get("finale_total"), delta=new.get("delta"))
                receipts[tx] = new
    missing_ids = [tx for tx in ids if tx not in receipts]
    reasons: dict[str, dict] = {}
    err = None if configured else "Finale not configured"
    if missing_ids and configured:
        try:
            with _cache_lock:
                po_map = dict(_cache["po_provinces"])
            dry = push_finale_invoices(invoices, po_map, live=False, only=missing_ids, client=client)
            reasons = {str(r.get("transaction_id")): r for r in dry.get("results") or []}
        except Exception as exc:  # noqa: BLE001 -- the digest still goes out
            err = f"Finale check failed: {str(exc)[:120]}"
    now = datetime.now(timezone.utc)
    out = {"floor": floor, "stale_days": stale_days, "missing": [], "deltas": [], "drafts": [], "by_tx": {}, "rows": []}
    for i in scoped:
        tx = str(i["transaction_id"])
        rec = receipts.get(tx)
        if rec is None and reasons.get(tx, {}).get("external"):
            ext = reasons[tx]["external"]
            rec = {**ext, "status": "external"}
        if rec is None:
            try:
                created = datetime.fromisoformat(str(i.get("created_at") or "").replace("Z", "+00:00"))
                days = (now - created).days
            except ValueError:
                days = None
            r = reasons.get(tx)
            st = (r or {}).get("status")
            stale = bool(stale_days is not None and days is not None and days >= stale_days
                         and st in (None, "skipped_not_shipped"))
            # Waiting on the warehouse (not shipped yet, or shipped and about to be
            # invoiced) is the pipeline's normal state -- information, not an issue --
            # until it has waited too long. A real failure needs attention now.
            waiting = st in ("skipped_not_shipped", "built") or (r is None and err is None)
            reason = _missing_reason(r, err)
            out["missing"].append({"invoice_number": i.get("invoice_number"), "po_number": i.get("po_number"),
                                   "province": i.get("province"), "days": days, "stale": stale,
                                   "attention": bool(stale or not waiting), "reason": reason})
            out["rows"].append({"invoice_number": i.get("invoice_number"), "po_number": i.get("po_number"),
                                "status": "missing", "hd_total": i.get("total_amount"),
                                "note": reason + (f" — accepted {days} days ago" if days is not None else "")})
            continue
        out["by_tx"][tx] = rec
        base = {"invoice_number": i.get("invoice_number"), "po_number": i.get("po_number"),
                "finale_id": rec.get("invoice_id_user") or rec.get("invoice_id"), "created_by": rec.get("created_by")}
        if rec.get("status") == "draft":
            out["drafts"].append(base)
        d = rec.get("delta")
        if d is not None and abs(d) > 0.01:
            out["deltas"].append({**base, "delta": d, "finale_total": rec.get("finale_total"),
                                  "hd_total": i.get("total_amount")})
        status = {"external": "by hand", "posted": "posted", "draft": "draft"}.get(str(rec.get("status")), str(rec.get("status")))
        note = {"draft": "held as draft: did not tie to the 810 or shipped qty differs — review in Finale",
                "external": "keyed by hand, not by the app"}.get(str(rec.get("status")), "")
        out["rows"].append({**base, "status": status, "delta": d, "finale_total": rec.get("finale_total"),
                            "hd_total": i.get("total_amount"), "note": note})
    return out


def _missing_reason(r: dict | None, err: str | None) -> str:
    """One plain phrase for why an SO has no Finale invoice, from the dry-run row."""
    if r is None:
        return err or "not checked"
    st = str(r.get("status") or "")
    if st == "skipped_not_shipped":
        return "not shipped in Finale"
    if st == "skipped_no_order":
        return "no Finale order"
    if st == "built":
        return "shipped in Finale — the next poll invoices it" + (" (as a draft)" if r.get("would") == "draft" else "")
    if st == "failed":
        return f"create failed: {r.get('error') or ''}".strip()
    return str(r.get("error") or st)
