import asyncio
import contextlib
import html
import os
import pathlib
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import BaseModel, Field

from app.crstl import CrstlClient
from app import tracking
from app.mail import send_mail, MailConfigError
from app.netsuite import transform_invoice, resolve_customer, external_id_for
from app.netsuite_csv import build_netsuite_csv
from app.netsuite_push import push_invoices, eligible_for_push, select_for_automation
from app.finale_invoice import push_finale_invoices
from app.shipstation import ShipStationClient, push_shipstation_close
from app.alerts import alert_day, alert_recipients, run_alerts, sent_asn_pos
from app.dropship import push_dropship_prefill
from app.finale_nonedi import push_nonedi_invoices
from app.finale_dsd import push_dsd_prefill, select_dsd_asns
from app.netsuite_payload import load_refs
from app.report import (XLSX_MEDIA_TYPE, dates_for, flavor_of, product_for,
                        rows_for_transactions, window_label, workbook_bytes)
from app.finale import FinaleClient
from app.shipments import merge_asn_dates, merge_finale_ship_dates


def load_env(path: str = ".env") -> None:
    env_file = pathlib.Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


load_env()

# po_provinces is kept, not just applied to the invoices, because the workbook
# recovers a Dropship invoice's province from its 850 the same way -- and
# rebuilding that map at export time would crawl every 850 on record again.
_cache: dict = {"invoices": [], "last_synced": None, "status": "never", "po_provinces": {}}
_cache_lock = threading.Lock()
_netsuite_state: dict = {"last_generated": None, "path": None, "count": 0, "skipped": 0, "error": None, "generating": False}
_netsuite_lock = threading.Lock()
# The REST push (POST /api/netsuite) is separate from the CSV export above. This
# holds the last run's summary + per-invoice results so the dashboard can show a
# push log; `running` guards against overlapping pushes.
_netsuite_push_state: dict = {"last_run": None, "mode": None, "summary": None,
                              "unresolved": [], "results": [], "blocked": None,
                              "error": None, "running": False}
_netsuite_push_lock = threading.Lock()
_digest_state: dict = {"last_sent": None, "count": 0, "error": None, "sending": False}
_digest_lock = threading.Lock()
_scheduler = None  # AsyncIOScheduler, set in lifespan; used to read next-run times


def _build_mock_po_provinces() -> dict[str, dict]:
    provinces = ["ON", "BC", "QC", "AB", "SK", "MB", "NS", "NB", "NL", "PE", "NT", "YT", "NU"]
    result = {}
    for i in range(50):
        po = f"PO-{98801 - i * 3}"
        # A blind every seventh PO, so the mock dashboard exercises the
        # Product column instead of showing one value down the whole sheet.
        items = ["138VB48D36WHTC"] if i % 7 == 0 else [f"72{135 + i % 40}-109-52-84-404"]
        # Every ninth PO has no ASN, so the mock exercises a blank date
        # column as well as a filled one.
        ship = {} if i % 9 == 0 else {"asn_date": f"2026-08-{(i % 27) + 1:02d}"}
        # Finale answers a day later, so the mock shows the two columns
        # disagreeing the way they do in production.
        if ship and i % 3 == 0:
            ship["finale_ship_date"] = f"2026-08-{(i % 27) + 2:02d}"
        if i % 10 < 2:
            result[po] = {"province": "ON", "store": "VAUGHAN", "vendor_items": items, **ship}
        elif i % 10 < 4:
            result[po] = {"province": "AB", "store": "CALGARY", "vendor_items": items, **ship}
        else:
            result[po] = {"province": provinces[i % len(provinces)], "store": None,
                          "vendor_items": items, **ship}
    return result


# Province map for MOCK_DATA mode (cycles through stores + provinces for 50 mock invoices)
_MOCK_PO_PROVINCES = _build_mock_po_provinces()


def _get_client() -> CrstlClient:
    return CrstlClient(
        base_url=os.environ.get("CRSTL_BASE_URL", "https://api.crstl.so/v2"),
        api_key=os.environ.get("CRSTL_API_KEY", ""),
    )

def _generate_mock_invoices(count: int = 50) -> list[dict]:
    _SERVICES = [
        ("Window Treatment Installation", 350.00),
        ("Drapery Installation", 325.00),
        ("Motorized Blinds Installation", 600.00),
        ("Sheer Curtain Installation", 200.00),
        ("Custom Roller Shades", 350.00),
        ("Roman Shade Installation", 275.00),
        ("Vertical Blind Installation", 180.00),
        ("Cornice Board Installation", 420.00),
        ("Plantation Shutter Installation", 550.00),
        ("Solar Screen Installation", 230.00),
    ]
    _PARTNERS = ["Home Depot", "Lowe's", "Costco"]
    # The workbook's Type column reads this. Without it mock mode exercised the
    # export with every row typed "Unknown", which is not a shape production
    # ever produces.
    _FLAVORS = ["HD Canada Dropship", "HD Canada DSD", "HD Canada Wholesale"]
    # Mirror the states Crstl actually returns. "Open"/"Completed" were invented
    # here and appear nowhere in real data; using them meant mock mode exercised
    # a status vocabulary production never sees, so the Accepted-only reporting
    # filter passed its tests while dropping every mock invoice. Live ratio on
    # 2026-08-24 was 107 Accepted to 43 Draft.
    _STATUSES = ["Accepted", "Accepted", "Accepted", "Draft"]
    _HARDWARE = ("Hardware / Materials", 1, 85.00)

    invoices = []
    base_inv = 2025_041
    base_po = 98_801
    # Spread invoices over the past ~10 weeks (one roughly every 1.5 days)
    base_day = date(2026, 7, 8)

    for i in range(count):
        idx = i + 1
        inv_date = base_day - timedelta(days=i + (i // 7))  # small gaps weekends
        due_date = inv_date + timedelta(days=30)
        created_hour = 8 + (idx % 10)
        service_idx = i % len(_SERVICES)
        svc_name, unit_price = _SERVICES[service_idx]
        qty = 2 + (i % 18)
        line_amount = round(unit_price * qty, 2)
        hw_amount = round(_HARDWARE[2] * (1 + i % 3), 2)
        subtotal = round(line_amount + hw_amount, 2)
        tax = round(subtotal * 0.075, 2)
        total = round(subtotal + tax, 2)
        invoices.append({
            "transaction_id": f"mock-{idx:03d}",
            "source_document_id": f"mock-src-{idx:03d}",
            "invoice_number": f"INV-2025-{base_inv - i:03d}",
            "po_number": f"PO-{base_po - i * 3}",
            "trading_partner": _PARTNERS[i % len(_PARTNERS)],
            "trading_partner_flavor": _FLAVORS[i % len(_FLAVORS)],
            "invoice_date": inv_date.isoformat(),
            "due_date": due_date.isoformat(),
            "status": _STATUSES[i % len(_STATUSES)],
            "subtotal": subtotal,
            "tax_amount": tax,
            "total_amount": total,
            "currency": "USD",
            "created_at": f"{inv_date.isoformat()}T{created_hour:02d}:00:00Z",
            "invoice_lines": [
                {"description": svc_name, "quantity": qty, "line_amount": line_amount},
                {"description": _HARDWARE[0], "quantity": _HARDWARE[1], "line_amount": hw_amount},
            ],
        })
    return invoices


_MOCK_INVOICES = _generate_mock_invoices(50)


def _merge_finale(po_index: dict) -> None:
    """Fill DSD ship dates from Finale, or leave them blank.

    A DSD ASN carries a pickup date and no ship date, so Finale is the only
    source for the actual departure. It is also a second external service on
    the sync path, so every failure here is swallowed: a blank Ship Date is a
    gap accounting can see, while a failed sync would take the whole dashboard
    down for a column that did not exist last week.
    """
    try:
        if not FinaleClient.configured():
            print("Finale not configured — DSD ship dates will be blank")
            return
        merge_finale_ship_dates(po_index, FinaleClient().fetch_ship_dates())
    except Exception as exc:
        # Broad by intent, and the configured() check is inside it: nothing
        # about this column is worth failing a sync for.
        print(f"WARNING: Finale ship dates unavailable, leaving them blank: {exc}")


def _attach_provinces(invoices: list[dict], po_provinces: dict[str, dict]) -> None:
    """Attach ship-to province + store from the source 850 to each invoice (factual, no rate math)."""
    for inv in invoices:
        loc = po_provinces.get(inv.get("po_number", ""), {})
        inv["province"] = loc.get("province")
        inv["store"] = loc.get("store")
        # Product (Blind / Drape Panel / Mixed / Unknown) from the 850 vendor item
        # numbers -- drives dropship customer routing (blinds vs panels store).
        inv["product"] = product_for(flavor_of(inv), loc)


# What we charge HD per ship-to province, used ONLY to annotate an
# unreconciled invoice with a "likely this tax at this rate" hint. This is NOT
# a substitute for the actual tax value from HD; it's a cross-check helper for
# accounting because Crstl's public API strips TXI segments from dropship 810s
# (their UI shows the value; their JSON does not — support ticket filed).
# Remove this once Crstl exposes TXI in generic_json_edi.
#
# Source: accounting's rate sheet, 2026-09-03. These are the rates we charge,
# not the rates a province levies, and the two differ:
#
#   SK  we charge PST. 5% GST + 6% PST = 11%.
#   BC  we do NOT charge PST, so BC is GST-only at 5% even though BC levies
#       PST at 7%. Dropship 810s have been arriving with an ST segment of 7%
#       anyway; Crstl has been asked to remove it and the fix may not have
#       landed yet, so a BC invoice may still read 12% until it does. Leave
#       this at 5% regardless -- the sheet is what we charge, and moving it to
#       match the bad data would make the error permanent.
#
#       Note this table does not reach the workbook. app/report.py reads the
#       TXI segment HD sent, so a BC invoice carrying the extra 7% reconciles
#       to the cent and appears in the export as ordinary tax -- the export
#       reports what was transmitted and does not check it against a rate.
#       This table only feeds the dashboard's Suggested Tax hint.
_PROVINCE_TAX_RATES: dict[str, tuple[str, float]] = {
    "AB": ("GST", 0.05),   "BC": ("GST", 0.05),   "MB": ("GST", 0.05),
    "YT": ("GST", 0.05),   "NT": ("GST", 0.05),   "NU": ("GST", 0.05),
    "SK": ("GST+PST", 0.11),
    "ON": ("HST", 0.13),
    "NB": ("HST", 0.15),   "NL": ("HST", 0.15),   "PE": ("HST", 0.15),
    "NS": ("HST", 0.14),   # config sets NS at 14% — trust the config
    "QC": ("GST+QST", 0.14975),
}


def _annotate_tax_suggestion(invoices: list[dict]) -> None:
    """For unreconciled invoices, add a `tax_suggestion` hint when the residual
    matches the ship-to province's standard rate within tolerance. Does NOT
    mutate `tax_amount`, `tax_breakdown`, or `discrepancy` — the raw Crstl
    values remain untouched. Accounting uses the hint to decide "this residual
    is expected tax per rate" vs "this is a real HD data error worth chasing"."""
    for inv in invoices:
        residual = round(inv.get("discrepancy") or 0.0, 2)
        if residual <= 0.01:  # only positive residuals could be missing tax
            continue
        province = inv.get("province")
        rate_info = _PROVINCE_TAX_RATES.get(province) if province else None
        if not rate_info:
            continue
        kind, rate = rate_info
        net_taxable = (
            (inv.get("subtotal") or 0.0)
            - (inv.get("allowance_amount") or 0.0)
            - (inv.get("discount_amount") or 0.0)
            + (inv.get("freight_amount") or 0.0)
            + (inv.get("fee_amount") or 0.0)
        )
        if net_taxable <= 0:
            continue
        expected = round(net_taxable * rate, 2)
        # Tolerance: 2 cents floor, 0.2% of net for larger invoices. Handles
        # cent-rounding on individual line items without matching random noise.
        tolerance = max(0.02, round(0.002 * net_taxable, 2))
        if abs(expected - residual) <= tolerance:
            inv["tax_suggestion"] = {
                "kind": kind,
                "rate": rate,
                "amount": expected,
                "province": province,
            }


def _mock_mode() -> bool:
    return os.environ.get("MOCK_DATA", "").lower() in ("1", "true", "yes")


def _refresh_cache() -> None:
    if _mock_mode():
        invoices = list(_MOCK_INVOICES)
        _attach_provinces(invoices, _MOCK_PO_PROVINCES)
        with _cache_lock:
            _cache["invoices"] = invoices
            _cache["po_provinces"] = dict(_MOCK_PO_PROVINCES)
            _cache["last_synced"] = datetime.now(timezone.utc).isoformat()
            _cache["status"] = "ok (mock)"
        return
    try:
        client = _get_client()
        invoices = client.fetch_invoices()
        po_provinces = client.fetch_po_provinces()
        # The Ship Date and Pickup Date columns come from the 856, joined on
        # the same PO number. Narrowed to POs already on file so the sync
        # does not crawl ASN details for orders the report will never show.
        merge_asn_dates(po_provinces, client.fetch_asn_dates(po_provinces.keys()))
        _merge_finale(po_provinces)
        _attach_provinces(invoices, po_provinces)
        _annotate_tax_suggestion(invoices)
        with _cache_lock:
            _cache["invoices"] = invoices
            _cache["po_provinces"] = po_provinces
            _cache["last_synced"] = datetime.now(timezone.utc).isoformat()
            _cache["status"] = "ok"
    except Exception as exc:
        with _cache_lock:
            _cache["status"] = f"error: {exc}"


class ReportUnavailable(RuntimeError):
    """The workbook could not be built because Crstl returned nothing usable."""


def _mock_report_row(inv: dict) -> dict:
    """MOCK_DATA only: a workbook row from a cached invoice dict.

    Mock invoices have no 810 payload to extract from, so without this the mock
    dashboard's Export button would have nothing to build from. Production must
    never take this path: the cached dicts read tax differently from
    app/report.py (see its module docstring), and letting both reach the
    workbook would put two different tax readings behind one filename.
    """
    subtotal = round(inv.get("subtotal") or 0.0, 2)
    tax = round(inv.get("tax_amount") or 0.0, 2)
    stated = round(inv.get("total_amount") or 0.0, 2)
    computed = round(subtotal + tax, 2)
    return {
        "transaction_id": inv.get("transaction_id", ""),
        "invoice": inv.get("invoice_number", ""),
        "date": inv.get("invoice_date", ""),
        "po": inv.get("po_number", ""),
        "flavor": flavor_of(inv),
        "product": product_for(flavor_of(inv), _MOCK_PO_PROVINCES.get(inv.get("po_number", ""))),
        "province": inv.get("province") or "",
        **dates_for(flavor_of(inv),
                    (_MOCK_PO_PROVINCES.get(inv.get("po_number", "")) or {}).get("asn_date", ""),
                    (_MOCK_PO_PROVINCES.get(inv.get("po_number", "")) or {}).get("finale_ship_date", "")),
        "subtotal": subtotal,
        "deductions": {},
        "charges": {},
        "taxes": {"GST": tax} if tax else {},
        "unknown": {},
        "stated": stated,
        "computed": computed,
        "variance": round(stated - computed, 2),
    }


def _workbook_for(invoices: list[dict]) -> bytes:
    """The accounting workbook for a set of cached invoices, as .xlsx bytes.

    Re-reads each invoice's raw 810 from Crstl rather than using the cached
    figures, because the cache and the workbook disagree on tax — the cache
    classifies SAC by code before indicator and never reads TXI at all, so
    Dropship tax there is inferred from a province rate table rather than
    reported. app/report.py's module docstring has the detail. The province
    map is the one `_refresh_cache` already built, so no 850 is re-crawled.
    """
    if _mock_mode():
        rows = sorted((_mock_report_row(inv) for inv in invoices),
                      key=lambda r: (r["flavor"], r["invoice"]))
    else:
        with _cache_lock:
            po_index = dict(_cache["po_provinces"])
        ids = [inv["transaction_id"] for inv in invoices if inv.get("transaction_id")]
        rows = rows_for_transactions(_get_client(), ids, po_index)
        if ids and not rows:
            # Every detail fetch failed. An empty workbook would read as "a
            # quiet day" to whoever opens it, which is the one thing it must
            # not do.
            raise ReportUnavailable(
                f"Crstl returned no detail for any of the {len(ids)} invoices requested."
            )
    return workbook_bytes(rows, window_label(rows))


def _generate_netsuite_export() -> None:
    """Build NetSuite CSV from the cached invoices. Province/store are already
    attached to each invoice by `_refresh_cache` — no extra API round-trips."""
    with _cache_lock:
        invoices = list(_cache["invoices"])

    if not invoices:
        print("NetSuite export: cache empty, skipping")
        return

    records, skipped = [], []
    for inv in invoices:
        try:
            line_items = transform_invoice(inv, province=inv.get("province"), store=inv.get("store"))
        except ValueError as exc:   # unsafe source_document_id etc. -- skip, never crash the export
            print(f"NetSuite export: skipped {inv.get('invoice_number', '?')} -- {exc}")
            skipped.append(inv.get("po_number", "?"))
            continue
        if line_items is None:
            skipped.append(inv.get("po_number", "?"))
        else:
            records.extend(line_items)

    if skipped:
        print(f"WARNING: NetSuite export skipped {len(skipped)} invoices (no province mapping): {skipped}")

    csv_bytes = build_netsuite_csv(records)
    out_path = pathlib.Path(__file__).parent.parent / ".tmp" / f"netsuite_export_{date.today().isoformat()}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(csv_bytes)

    with _netsuite_lock:
        _netsuite_state["last_generated"] = datetime.now(timezone.utc).isoformat()
        _netsuite_state["path"] = str(out_path)
        _netsuite_state["count"] = len(records)
        _netsuite_state["skipped"] = len(skipped)
        _netsuite_state["error"] = None

    print(f"NetSuite export: {len(records)} invoices → {out_path} ({len(skipped)} skipped)")


# Only invoices HD has acknowledged are reported. The warehouse resubmits when
# it catches a mistake, and every resubmission lands as another Crstl record --
# measured 2026-08-24: 36 redundant records across 150 invoices, 31 of them
# Drafts shadowing an Accepted twin. Reporting Drafts hands accounting the same
# invoice several times, which is a double-entry risk, not just noise.
#
# This is an allowlist, not a "skip Draft" rule: an unrecognised state must not
# quietly reach accounting. The trade-off is that a NEW good state would be
# filtered out instead, so _reportable logs anything it drops that is not a
# known non-terminal state. Observed: "Accepted" (terminal, reportable), "Draft"
# and "Send_Success" (both non-terminal -- a Send_Success 810 has been transmitted
# to HD but not yet acknowledged, so it is deferred like a Draft until it becomes
# Accepted). We only book invoices HD has ACCEPTED.
REPORTABLE_STATUSES = frozenset({"Accepted"})
_KNOWN_UNREPORTABLE = frozenset({"Draft", "Send_Success"})


def _reportable(invoices: list[dict]) -> list[dict]:
    """Invoices fit to report to accounting. A Draft that is later accepted is
    picked up by the next digest: it is never marked emailed while filtered, so
    nothing is lost, only deferred until HD acknowledges it."""
    keep, dropped = [], {}
    for inv in invoices:
        status = inv.get("status") or ""
        if status in REPORTABLE_STATUSES:
            keep.append(inv)
        elif status not in _KNOWN_UNREPORTABLE:
            dropped[status] = dropped.get(status, 0) + 1
    if dropped:
        # Loud on purpose. If Crstl introduces a state that means "good", these
        # invoices would silently stop reaching accounting; this is the warning
        # that says to add it to REPORTABLE_STATUSES.
        print(f"WARNING: withheld invoices with unrecognised status {dropped} — "
              f"if one of these is reportable, add it to REPORTABLE_STATUSES")
    return keep


def _netsuite_base_url() -> str:
    """Base URL for direct links to NetSuite records, from the account id."""
    acct = os.environ.get("NETSUITE_ACCOUNT_ID", "").strip()
    return f"https://{acct}.app.netsuite.com" if acct else ""


def _so_link(netsuite_id) -> str:
    """A direct link to the sales order in NetSuite, or "" if id/account is missing."""
    base = _netsuite_base_url()
    return f"{base}/app/accounting/transactions/salesord.nl?id={netsuite_id}" if (base and netsuite_id) else ""


def _so_digest_data() -> dict:
    """Receipt-sourced view for the accounting SO digest -- what actually happened,
    not in-memory state. Scoped to the go-live cutoff. Returns the day's NEW SOs
    (pushed to NetSuite, not yet reported) and the GAPS (accepted + eligible but no
    SO created), plus a dry-run row per new SO for its numbers + reconcile flag."""
    with _cache_lock:
        invoices = list(_cache["invoices"])
    cutoff = str((load_refs().get("automation") or {}).get("go_live_after") or "")
    scoped = [i for i in eligible_for_push(invoices)
              if str(i.get("invoice_date") or "")[:10] >= cutoff]
    events = tracking.get_latest_events([str(i["transaction_id"]) for i in scoped])

    def ev(i, k):
        return events.get(str(i["transaction_id"]), {}).get(k)

    new_sos = [i for i in scoped if ev(i, "netsuite_at") and not ev(i, "so_digest_at")]
    # No SO in NetSuite is only a problem once the scheduled push has had its chance:
    # an 810 accepted AFTER the last push run is simply waiting for tonight's.
    last_push = _last_netsuite_push_at()
    push_dt = _parse_iso(last_push)
    gaps_all = [i for i in scoped if not ev(i, "netsuite_at")]

    def accepted_after_push(i) -> bool:
        created = _parse_iso(i.get("created_at"))
        return bool(push_dt and created and created >= push_dt)
    gaps = [i for i in gaps_all if not accepted_after_push(i)]
    gaps_waiting = [i for i in gaps_all if accepted_after_push(i)]
    dry = (push_invoices(invoices, live=False,
                         only=[str(i["transaction_id"]) for i in new_sos])["results"]
           if new_sos else [])
    row_by_tx = {str(r["transaction_id"]): r for r in dry}
    eids = {}
    for i in new_sos:
        try:
            eids[str(i["transaction_id"])] = external_id_for(i)
        except Exception:
            pass
    ns_ids = tracking.get_netsuite_ids(list(eids.values()))
    # invoice_number -> (SO-created date, direct SO link) for the Excel's SO column.
    so_map = {}
    for i in new_sos:
        tx = str(i["transaction_id"])
        created = (events.get(tx, {}).get("netsuite_at") or "")[:10]
        so_map[str(i.get("invoice_number"))] = (created, _so_link(ns_ids.get(eids.get(tx))))
    # Finale invoices created for these SOs (receipts), for the headline line, the
    # Excel column and the issues list. Empty when Finale invoicing is off.
    fin_cfg = _finale_config()
    # Non-EDI receipts since the last digest that was actually SENT (a digest fires
    # after each push as well as at 7:15), so a figure is never re-reported as new.
    since = _last_digest_sent_at() or (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    nonedi = [r for r in tracking.recent_finale_invoices(since) if str(r.get("key", "")).startswith("order:")]
    # The reconciliation runs FIRST: it may refresh a draft receipt that has since
    # been posted by hand, and the headline must show that state.
    recon = _finale_reconciliation(invoices, fin_cfg.get("stale_pickup_days")) if _finale_enabled() else _EMPTY_RECON
    finale = tracking.get_finale_invoices([str(i["transaction_id"]) for i in new_sos])
    # A new SO whose Finale invoice someone keyed by hand has no receipt until the
    # next poll runs; the reconciliation already read it -- show it now.
    for i in new_sos:
        tx = str(i["transaction_id"])
        if tx not in finale and tx in recon["by_tx"]:
            finale[tx] = recon["by_tx"][tx]
    return {"cutoff": cutoff, "new_sos": new_sos, "gaps": gaps, "gaps_waiting": gaps_waiting,
            "last_push": last_push,
            "row_by_tx": row_by_tx, "eids": eids, "ns_ids": ns_ids, "so_map": so_map,
            "finale": finale, "finale_enabled": _finale_enabled(),
            "nonedi": nonedi, "nonedi_since": since, "recon": recon}


def _last_netsuite_push_at() -> str | None:
    """When the NetSuite push last had its chance, UTC ISO, or None if it never has.
    The LATER of: the last scheduled job run of ANY status (a run that skipped
    because auto-push is off still means every 810 accepted before it is a real
    gap, not a waiting one), and the start of the last live push in this process
    (set before the post-push digest fires, so the 810s that push just failed on
    are not mistaken for 'accepted after the push')."""
    runs = tracking.recent_job_runs(limit=1, job="netsuite_push")
    logged = runs[0].get("ran_at") if runs else None
    with _netsuite_push_lock:
        live = _netsuite_push_state.get("last_run") if _netsuite_push_state.get("mode") == "live" else None
    return max(x for x in (logged, live) if x) if (logged or live) else None


def _parse_iso(iso) -> datetime | None:
    """A UTC ISO timestamp (with or without 'Z' / fraction / offset) as an aware
    datetime, or None. Source formats differ (CRSTL 'Z', sqlite '+00:00'), so
    never compare them as strings."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _et(iso: str | None) -> str:
    """A UTC ISO timestamp as 'YYYY-MM-DD HH:MM ET' for the email."""
    if not iso:
        return ""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("America/Toronto")).strftime("%Y-%m-%d %H:%M ET")
    except ValueError:
        return str(iso)


def _last_digest_sent_at() -> str | None:
    """When the last digest email actually went out (job_runs), or None."""
    for run in tracking.recent_job_runs(limit=20, job="daily_digest"):
        if run.get("status") == "ok" and not str(run.get("detail") or "").startswith("nothing to report"):
            return run.get("ran_at")
    return None


def _so_digest_rows(data: dict) -> list[dict]:
    """One display row per new SO: invoice, NetSuite customer + product, total,
    reconcile flag, and a direct SO link."""
    rows = []
    for i in data["new_sos"]:
        tx = str(i["transaction_id"])
        r = data["row_by_tx"].get(tx, {})
        ns_id = data["ns_ids"].get(data["eids"].get(tx))
        rows.append({
            "invoice_number": i.get("invoice_number"),
            "po_number": i.get("po_number"),
            "customer": r.get("customer_name") or (_netsuite_customer(i) or {}).get("name"),
            "province": i.get("province") or r.get("where"),
            "channel": r.get("channel") or ("dsd" if i.get("store") else "dropship"),
            "product": i.get("product"),
            "total": r.get("total"),
            "reconcile_flag": r.get("reconcile_flag"),
            "so_link": _so_link(ns_id),
            "finale_status": (data.get("finale") or {}).get(tx, {}).get("status"),
            "finale_id": (data.get("finale") or {}).get(tx, {}).get("invoice_id_user"),
            "finale_by": (data.get("finale") or {}).get(tx, {}).get("created_by"),
            "finale_delta": (data.get("finale") or {}).get(tx, {}).get("delta"),
        })
    return rows


def _so_digest_html(data: dict, rows: list[dict]) -> str:
    """The accounting email: a headline, the Blinds-vs-Drapes summary table, and --
    only when there is something accounting must act on -- ONE 'Needs attention'
    table: an 810 with no SO in NetSuite after the push has had its chance, or an
    SO whose total is off its 810. Nothing about Finale: that is not accounting's
    concern and lives on the Excel's Finale sheet (Ritchie, 2026-09-16)."""
    total = sum((r["total"] or 0) for r in rows)
    # Channel x product, with a subtotal per channel (DSD first) and a grand total.
    label = {"dsd": "DSD", "dropship": "Dropship"}
    by_ch: dict[str, dict[str, list]] = {}
    for r in rows:
        ch = label.get(str(r.get("channel") or ""), str(r.get("channel") or "—"))
        d = r["product"] or "—"
        cell = by_ch.setdefault(ch, {}).setdefault(d, [0, 0.0])
        cell[0] += 1; cell[1] += (r["total"] or 0)
    order = sorted(by_ch, key=lambda c: (c != "DSD", c != "Dropship", c))
    body_rows = ""
    for ch in order:
        prods = by_ch[ch]
        for k, (cnt, val) in sorted(prods.items()):
            body_rows += (f'<tr><td>{html.escape(ch)}</td><td>{html.escape(k)}</td><td align="right">{cnt}</td>'
                          f'<td align="right">${val:,.2f}</td></tr>')
        if len(order) > 1:
            body_rows += (f'<tr style="background:#f6f8fb;font-weight:bold"><td>{html.escape(ch)} total</td><td></td>'
                          f'<td align="right">{sum(c for c, _ in prods.values())}</td>'
                          f'<td align="right">${sum(v for _, v in prods.values()):,.2f}</td></tr>')
    product_table = (
        '<table cellpadding="6" cellspacing="0" border="1" '
        'style="border-collapse:collapse;font-size:13px;margin:6px 0 14px">'
        '<tr style="background:#1f3a5f;color:#ffffff"><th align="left">Channel</th><th align="left">Product</th>'
        '<th align="right">SOs</th><th align="right">Value (CAD)</th></tr>'
        + body_rows
        + f'<tr style="background:#eef2f7;font-weight:bold"><td>Total</td><td></td>'
          f'<td align="right">{len(rows)}</td><td align="right">${total:,.2f}</td></tr></table>')

    h = f"<p><strong>{len(rows)} sales order(s)</strong> created in NetSuite.</p>" + product_table

    issues = []
    for g in data["gaps"]:
        issues.append((g.get("invoice_number"), g.get("po_number"), g.get("province"), g.get("product"),
                       g.get("total_amount"), "No SO in NetSuite"))
    for r in rows:
        if r["reconcile_flag"]:
            issues.append((r["invoice_number"], r.get("po_number"), r.get("province"), r.get("product"),
                           r["total"], f"SO {r['reconcile_flag']}"))
    if issues:
        cell = lambda v, align="left": f'<td align="{align}">{html.escape(str(v if v not in (None, "") else "—"))}</td>'
        body_rows = "".join(
            "<tr>" + cell(inv) + cell(po) + cell(prov) + cell(prd)
            + f'<td align="right">${(amt or 0):,.2f}</td>' + cell(why) + "</tr>"
            for inv, po, prov, prd, amt, why in issues[:200])
        h += ('<h3 style="color:#b32020;margin-top:16px">Needs attention</h3>'
              '<table cellpadding="6" cellspacing="0" border="1" '
              'style="border-collapse:collapse;font-size:13px;margin:6px 0 14px">'
              '<tr style="background:#b32020;color:#ffffff"><th align="left">Invoice</th><th align="left">PO</th>'
              '<th align="left">Province</th><th align="left">Product</th><th align="right">Total</th>'
              '<th align="left">Problem</th></tr>' + body_rows + "</table>")
    return h


def _so_digest_workbook(new_sos: list[dict], so_map: dict, finale_map: dict | None = None,
                        finale_rows: list[dict] | None = None, nonedi: list[dict] | None = None) -> bytes:
    """The accounting Excel: the SAME export workbook (built from the 810s, so the
    figures match the Export button exactly) PLUS a 'Netsuite SO created' column whose
    cell links straight to each SO. so_map is {invoice_number: (created_date, so_url)}.
    finale_rows (the rolling Finale reconciliation) and nonedi (recent non-EDI Finale
    receipts) go on a second 'Finale' sheet -- for Ritchie and the other departments,
    never in the email body."""
    import io
    import re
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    if new_sos:
        wb = load_workbook(io.BytesIO(_workbook_for(new_sos)))
    else:
        wb = Workbook(); wb.active.title = "Invoices"; wb.active.append(["Invoice"])
        wb.active.auto_filter.ref = "A1:A1"
    ws = wb["Invoices"]
    col = ws.max_column + 1
    hdr = ws.cell(row=1, column=col, value="Netsuite SO created")
    hdr.fill = PatternFill("solid", fgColor="1F3864")
    hdr.font = Font(bold=True, color="FFFFFF", size=10)
    hdr.alignment = Alignment(horizontal="center", vertical="center")
    for r in range(2, ws.max_row + 1):
        inv = ws.cell(row=r, column=1).value
        if not inv or inv == "Total":
            continue
        created, url = so_map.get(str(inv), ("", ""))
        cell = ws.cell(row=r, column=col, value=created or "—")
        if url:
            cell.hyperlink = url
            cell.style = "Hyperlink"
    ws.column_dimensions[get_column_letter(col)].width = 20
    if finale_map is not None:
        # 'Finale invoice': the invoice id + posted/draft. No deep link -- Finale's
        # UI addresses invoices by an opaque token, not the id, so a built URL would
        # be a guess. by_inv is {invoice_number: (invoice_id_user, status)}.
        col += 1
        hdr = ws.cell(row=1, column=col, value="Finale invoice")
        hdr.fill = PatternFill("solid", fgColor="1F3864")
        hdr.font = Font(bold=True, color="FFFFFF", size=10)
        hdr.alignment = Alignment(horizontal="center", vertical="center")
        for r in range(2, ws.max_row + 1):
            inv = ws.cell(row=r, column=1).value
            if not inv or inv == "Total":
                continue
            fid, fstatus, fby, fdelta = (tuple(finale_map.get(str(inv), ())) + ("", "", None, None))[:4]
            label = f"{fid} ({'by hand' if fstatus == 'external' else fstatus}" + (f", {fby}" if fby and fstatus == "external" else "") + ")"
            ws.cell(row=r, column=col, value=(label if fid else "—"))
        ws.column_dimensions[get_column_letter(col)].width = 30
        col += 1
        hdr = ws.cell(row=1, column=col, value="Finale vs 810")
        hdr.fill = PatternFill("solid", fgColor="1F3864")
        hdr.font = Font(bold=True, color="FFFFFF", size=10)
        hdr.alignment = Alignment(horizontal="center", vertical="center")
        for r in range(2, ws.max_row + 1):
            inv = ws.cell(row=r, column=1).value
            if not inv or inv == "Total":
                continue
            fid, fstatus, fby, fdelta = (tuple(finale_map.get(str(inv), ())) + ("", "", None, None))[:4]
            ws.cell(row=r, column=col, value=("—" if not fid or fdelta is None else
                                              ("tied" if abs(fdelta) <= 0.01 else f"{fdelta:+.2f}")))
        ws.column_dimensions[get_column_letter(col)].width = 14
    m = re.match(r"A1:([A-Z]+)(\d+)", ws.auto_filter.ref or "")
    if m:  # extend the filter to cover the new column(s)
        ws.auto_filter.ref = f"A1:{get_column_letter(col)}{m.group(2)}"
    if finale_rows is not None:
        fs = wb.create_sheet("Finale")
        heads = ["Invoice", "PO", "Finale invoice", "Created by", "Status", "Finale total", "810 total", "Finale vs 810", "Note"]
        fs.append(heads)
        for c in range(1, len(heads) + 1):
            hc = fs.cell(row=1, column=c)
            hc.fill = PatternFill("solid", fgColor="1F3864"); hc.font = Font(bold=True, color="FFFFFF", size=10)
            hc.alignment = Alignment(horizontal="center", vertical="center")
        for r in finale_rows:
            d = r.get("delta")
            fs.append([r.get("invoice_number"), r.get("po_number"), r.get("finale_id") or "—", r.get("created_by") or "—",
                       r.get("status") or "—", r.get("finale_total"), r.get("hd_total"),
                       ("—" if d is None else ("tied" if abs(d) <= 0.01 else f"{d:+.2f}")), r.get("note") or ""])
        for r in nonedi or []:
            fs.append(["(non-EDI)", r.get("po_number"), r.get("invoice_id_user") or r.get("invoice_id"), r.get("created_by") or "—",
                       r.get("status") or "—", r.get("finale_total"), None, "—", "non-EDI order, no 810"])
        for c, w in zip("ABCDEFGHI", (16, 14, 18, 20, 10, 13, 13, 14, 60)):
            fs.column_dimensions[c].width = w
        fs.auto_filter.ref = f"A1:I{max(fs.max_row, 1)}"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _send_daily_digest(selected_ids: list[str] | None = None) -> dict:
    """The daily accounting digest: the sales orders created in NetSuite since the
    last digest (ready for invoice generation), a summary (total / province /
    product), and an issues callout. Sourced from durable push receipts, not
    in-memory state. Always sends -- accounting uses receipt as proof of life.

    Reported SOs are marked with a 'so_digest' event so they don't repeat tomorrow.
    (`selected_ids` is accepted for API compatibility but the digest is receipt-
    driven, so it is ignored.) Raises MailConfigError if recipients are unset.
    """
    recipients = [r.strip() for r in os.environ.get("MAIL_RECIPIENTS", "").split(",") if r.strip()]
    if not recipients:
        raise MailConfigError("MAIL_RECIPIENTS not set (comma-separated addresses)")

    data = _so_digest_data()
    rows = _so_digest_rows(data)
    today = date.today().isoformat()
    n = len(rows)
    subject = f"HD Sales Orders for invoicing — {today} — {n} SO(s)"
    recon = data.get("recon") or _EMPTY_RECON
    n_issues = len(data["gaps"]) + sum(1 for r in rows if r.get("reconcile_flag"))
    if n_issues:
        subject += f" · {n_issues} issue(s)"
    body_html = _so_digest_html(data, rows)

    attachments = None
    finale_rows = recon.get("rows") if data.get("finale_enabled") else None
    if data["new_sos"] or finale_rows or data.get("nonedi"):
        finale_by_inv = {str(i.get("invoice_number")): (f.get("invoice_id_user") or f.get("invoice_id") or "", f.get("status") or "",
                                                        f.get("created_by"), f.get("delta"))
                         for i in data["new_sos"]
                         for f in [data["finale"].get(str(i["transaction_id"]))] if f}
        attachments = [(f"hd_sales_orders_{today}.xlsx",
                        _so_digest_workbook(data["new_sos"], data["so_map"],
                                            finale_by_inv if (data.get("finale_enabled") or finale_by_inv) else None,
                                            finale_rows=(finale_rows if (finale_rows is not None or data.get("nonedi")) else None),
                                            nonedi=data.get("nonedi")),
                        XLSX_MEDIA_TYPE)]

    # Send FIRST; only mark reported once the mail is away, so a send failure leaves
    # the SOs to carry into tomorrow's digest rather than being silently dropped.
    send_mail(subject=subject, body_html=body_html, recipients=recipients, attachments=attachments)
    reported_ids = [str(i["transaction_id"]) for i in data["new_sos"]]
    if reported_ids:
        tracking.record_events(reported_ids, "so_digest")

    return {"sent_to": recipients, "count": n, "gaps": len(data["gaps"]),
            "subject": subject, "mode": "so_digest"}


AUTO_DIGEST_SETTING = "auto_digest_enabled"

# ---------------------------------------------------------------- Finale invoicing
# ONE tool invoices BOTH channels in Finale (internal record; goes nowhere
# downstream). It rides the NetSuite push event: the invoices that just landed as
# SOs get their Finale invoice in the same run, so both systems are created from
# one 810-Accepted moment. Two gates, BOTH must be on: config finale.enabled (ships
# OFF) and this runtime toggle (dashboard; default OFF). Manual: POST /api/finale.
AUTO_FINALE_SETTING = "auto_finale_enabled"
# DSD pickup numbers (PRO/RTS from the Accepted 856 onto the Finale shipment). Runs
# inside the Finale invoicing poll, so it is ALSO off whenever that job is off.
AUTO_DSD_SETTING = "auto_dsd_prefill_enabled"
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
_finale_run_lock = threading.RLock()
_finale_run_holder: Optional[str] = None


class FinaleBusy(RuntimeError):
    """Another Finale run holds the lock; nothing was done."""


@contextlib.contextmanager
def _finale_run(label: str):
    global _finale_run_holder
    if not _finale_run_lock.acquire(blocking=False):
        raise FinaleBusy(f"another Finale run is in progress ({_finale_run_holder or 'unknown'})")
    outer = _finale_run_holder is None
    if outer:
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
        _finale_run_lock.release()


def _finale_config() -> dict:
    return load_refs().get("finale") or {}


def _finale_enabled() -> bool:
    return bool(_finale_config().get("enabled")) and _job_enabled(AUTO_FINALE_SETTING, default="false")


def _run_finale_push(live: bool, ids: Optional[list[str]], limit: Optional[int],
                     max_per_run: Optional[int] = None) -> dict:
    """Create (live) or preview (dry) Finale invoices for cached invoices via the
    shared engine, and record the run. The engine enforces eligibility, the exact-
    cents build, the reconcile + qty gates (posted vs draft), idempotency and, for
    the automated callers that pass it, the blast cap on invoices about to be
    created. Manual runs (the endpoint) are uncapped, like manual NetSuite pushes."""
    with _cache_lock:
        invoices = list(_cache["invoices"])
        po_map = dict(_cache["po_provinces"])
    with _finale_run("edi"):
        result = push_finale_invoices(invoices, po_map, live=live, only=ids, limit=limit, max_per_run=max_per_run)
    with _finale_push_lock:
        _finale_push_state.update({
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
        with _finale_push_lock:
            _finale_push_state["error"] = str(exc)
        print(f"WARNING: Finale invoicing failed after NetSuite push: {exc}")
        tracking.record_job_run("finale_push", "error", str(exc)[:200])


def _refresh_new_accepted() -> int:
    """Incremental 810 refresh for the 15-minute Finale poll: ONE list call to read
    every transaction's state, detail fetches only for transactions that are new or
    whose state changed since the cache (e.g. Draft -> Accepted), plus the 850s of any
    PO the cache doesn't know. Merges into the cache; never raises. Returns how many
    invoices were refreshed. The 4:45 full refresh still owns ASN/Finale ship dates."""
    if _mock_mode():
        return 0
    try:
        client = _get_client()
        states = client.list_transaction_states()
        with _cache_lock:
            cached = {str(i.get("transaction_id")): str(i.get("status") or "") for i in _cache["invoices"]}
            po_map = dict(_cache["po_provinces"])
        changed = [tid for tid, st in states.items() if cached.get(tid) != st["state"]]
        if not changed:
            return 0
        fresh = client.fetch_invoices(only_ids=changed)
        missing_pos = sorted({str(i.get("po_number") or "") for i in fresh} - set(po_map) - {""})
        fetched_pos = client.fetch_po_provinces(only_pos=missing_pos) if missing_pos else {}
        po_map.update(fetched_pos)
        _attach_provinces(fresh, po_map)
        _annotate_tax_suggestion(fresh)
        by_id = {str(i.get("transaction_id")): i for i in fresh}
        with _cache_lock:
            merged = [by_id.pop(str(i.get("transaction_id")), i) for i in _cache["invoices"]]
            merged.extend(by_id.values())
            _cache["invoices"] = merged
            # Merge, don't replace: the 4:45 full refresh may have landed while this
            # poll was fetching, and its 850 map must not be overwritten by our copy.
            _cache["po_provinces"] = {**_cache["po_provinces"], **fetched_pos}
            _cache["last_incremental"] = datetime.now(timezone.utc).isoformat()
        return len(fresh)
    except Exception as exc:
        print(f"WARNING: incremental 810 refresh failed: {exc}")
        tracking.record_job_run("finale_push", "error", f"refresh: {str(exc)[:160]}")
        return 0


def _finale_edi_pass() -> None:
    """The EDI half of the poll: incremental 810 refresh, then invoice the Accepted
    810s not yet invoiced, under the automation date guards; the engine applies
    finale.max_per_run to the invoices it is about to create (a pending, unshipped
    810 is waiting, not writing). Early returns here only end THIS pass -- the
    non-EDI pass still runs after it."""
    _refresh_new_accepted()
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
                     max_per_run=_finale_config().get("max_per_run"))


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
    return _job_enabled(AUTO_DSD_SETTING, default="false")


def _run_dsd_prefill(live: bool, ids: Optional[list[str]], limit: Optional[int]) -> dict:
    """Copy PRO/RTS from Accepted DSD 856s onto their open Finale shipments (live) or
    preview it (dry). `ids` names ASN ids; a manual run may name any Accepted ASN, the
    automated pass (ids=None) applies the shared guards: go_live_after floor, the
    created_within_days window, max_per_run, and one receipt per ASN."""
    from app.finale import FinaleClient, FinaleUnavailable
    if not FinaleClient.configured():
        raise FinaleUnavailable("FINALE_* credentials not set")
    crstl = _get_client()
    fin, auto = _finale_config(), (load_refs().get("automation") or {})
    automated = ids is None
    if automated:
        states = crstl.list_transaction_states(transaction_type="856")
        todo = tracking.get_unprefilled_asn_ids(list(states))
        ids = select_dsd_asns(states, set(states) - set(todo),
                              created_after=_finale_floor(),
                              created_within_days=auto.get("created_within_days"))
    asns = crstl.fetch_asn_refs(ids) if ids else []
    with _finale_run("dsd"):
        result = push_dsd_prefill(asns, live=live, only=None, limit=limit, client=FinaleClient(),
                                  max_per_run=fin.get("max_per_run") if automated else None)
    blocked = result.get("blocked")
    with _finale_push_lock:
        _finale_push_state["dsd"] = {"last_run": datetime.now(timezone.utc).isoformat(), **result}
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
    with _finale_push_lock:
        _finale_push_state["shipstation"] = {"last_run": datetime.now(timezone.utc).isoformat(), **result}
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
    applies the label-date floor and the cap. Two listings, no per-order reads."""
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
    with _finale_run("dropship"):
        result = push_dropship_prefill(client.list_shipments(), labels, live=live, only=ids, limit=limit,
                                       client=client, carrier_url=carrier["url"] if carrier["enabled"] else None,
                                       created_after=(str(cfg.get("go_live_after") or "") or None) if automated else None,
                                       max_per_run=cfg.get("max_per_run") if automated else None)
    result["carrier"] = {"wanted": carrier["name"], "enabled": carrier["enabled"], "note": carrier["reason"] or None}
    blocked = result.get("blocked")
    with _finale_push_lock:
        _finale_push_state["dropship"] = {"last_run": datetime.now(timezone.utc).isoformat(), **result}
    c = result["summary"]
    tracking.record_job_run("dropship_prefill", "blocked" if blocked else ("ok" if not c.get("failed") else "partial"),
                            (f"{blocked} -- refusing; run manually" if blocked else
                             f"{c['candidates']} label(s): {c.get('prefilled', 0)} prefilled, {c.get('would_prefill', 0)} would, "
                             f"{c.get('skipped_equal', 0)} already set, {c.get('skipped_no_shipment', 0)} waiting, "
                             f"{c.get('skipped_shipped', 0)} shipped, {c.get('skipped_ambiguous', 0)} ambiguous, "
                             f"{c.get('failed', 0)} failed") + f" [{'live' if live else 'dry'}]")
    return result


def _alerts_config() -> dict:
    return load_refs().get("alerts") or {}


def _run_alerts_job() -> None:
    """Every 15 minutes as its OWN job -- not a pass of the Finale poll, so switching
    Finale invoicing off (or a manual Finale run holding its lock) never silences it:
    a monitor must not depend on what it monitors. Two gates: config alerts.enabled
    and the dashboard toggle. Weekdays only (Toronto), like the digest -- an outlier
    still open on Monday is emailed on Monday's first run."""
    if not _alerts_config().get("enabled"):
        tracking.record_job_run("order_alerts", "skipped", "disabled in config"); return
    if not _job_enabled(AUTO_ALERTS_SETTING):
        tracking.record_job_run("order_alerts", "skipped", "disabled"); return
    if not alert_day():
        tracking.record_job_run("order_alerts", "skipped", "weekend -- alerts resume Monday"); return
    try:
        _run_alerts(True)
    except Exception as exc:
        print(f"WARNING: order alerts run failed: {exc}")
        tracking.record_job_run("order_alerts", "error", str(exc)[:200])


def _run_alerts(live: bool) -> dict:
    """Find order outliers (today: ShipStation label with no CRSTL 856) and email the
    new ones to ALERT_RECIPIENTS in ONE message (live), or preview it (dry)."""
    from app.finale import FinaleUnavailable
    if not ShipStationClient.configured():
        raise FinaleUnavailable("SHIPSTATION_V1_KEY / SHIPSTATION_V1_SECRET not set")
    cfg = _alerts_config()
    since = (datetime.now(timezone.utc) - timedelta(days=int(cfg.get("lookback_days") or 7))).strftime("%Y-%m-%d")
    shipments = ShipStationClient().list_shipments(int(cfg.get("dropship_store_id") or 0), since)
    crstl = _get_client()
    asn_pos = sent_asn_pos(crstl.list_transaction_states("856"))       # Draft/Rejected do not count as sent
    orders_850 = [(tx.get("metadata") or tx) for tx in crstl._fetch_all_transactions("850")]
    po_ids = {str(m.get("reference_id")): str(m.get("id")) for m in orders_850}
    # The dropship/DSD split comes from CRSTL's own flavour on the 850, not the PO format.
    dropship_pos = {str(m.get("reference_id")) for m in orders_850
                    if "dropship" in str(m.get("trading_partner_flavor") or "").lower()}
    from app.finale import FinaleClient
    finale_shipments = FinaleClient().list_shipments() if FinaleClient.configured() else []
    result = run_alerts(shipments, asn_pos, po_ids, config=cfg, live=live, recipients=alert_recipients(),
                        send=send_mail, finale_shipments=finale_shipments, dropship_pos=dropship_pos)
    with _finale_push_lock:
        _finale_push_state["alerts"] = {"last_run": datetime.now(timezone.utc).isoformat(),
                                        **{k: v for k, v in result.items() if k != "body_html"}}
    c = result["summary"]
    tracking.record_job_run("order_alerts", "ok",
                            f"{c['found']} outlier(s): {c['new']} new{' (emailed)' if result['sent'] else ''}, "
                            f"{c['still_open']} still open, {c['resolved']} resolved [{'live' if live else 'dry'}]")
    return result


def _crstl_po_set() -> set:
    """Every PO number Crstl knows -- the 810s in the cache and the 850 map. A Finale
    sale order whose id is NOT in here is non-EDI (classification by exclusion)."""
    with _cache_lock:
        pos = {str(i.get("po_number") or "") for i in _cache["invoices"]}
        pos |= set(_cache["po_provinces"].keys())
    pos.discard("")
    return pos


def _run_nonedi_push(live: bool, ids: Optional[list[str]], limit: Optional[int]) -> dict:
    """Invoice (live) or preview (dry) shipped non-EDI sale orders in Finale. Reads the
    sale-order list + party provinces from Finale; the engine does the rest."""
    from app.finale import FinaleClient, FinaleUnavailable
    if not FinaleClient.configured():
        raise FinaleUnavailable("FINALE_* credentials not set")
    client = FinaleClient()
    fin = _finale_config()
    with _finale_run("nonedi"):
        result = push_nonedi_invoices(client.list_sale_orders(), _crstl_po_set(), live=live, only=ids, limit=limit,
                                      client=client, floor=str(fin.get("nonedi_go_live_after") or "") or None,
                                      max_per_run=fin.get("max_per_run") if ids is None else None,   # automation only
                                      auto_reopen=bool(fin.get("auto_reopen")))
    with _finale_push_lock:
        _finale_push_state["nonedi"] = {"last_run": datetime.now(timezone.utc).isoformat(), **result}
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


def _auto_digest_enabled() -> bool:
    """Runtime toggle read from the settings table. Defaults to enabled if the
    setting has never been set — production LXC just works after deploy."""
    return tracking.get_setting(AUTO_DIGEST_SETTING, "true").lower() != "false"


def _send_digest_safe(reason: str) -> bool:
    """Send the accounting digest and update digest state; never raises (best-effort).
    Returns True if it sent. The _digest_lock guard stops a post-push send and the
    scheduled run from overlapping. `reason` is logged so the run history shows what
    triggered each digest (post-push vs scheduled)."""
    with _digest_lock:
        if _digest_state.get("sending"):
            return False
        _digest_state["sending"] = True
    try:
        result = _send_daily_digest()
        with _digest_lock:
            _digest_state["last_sent"] = datetime.now(timezone.utc).isoformat()
            _digest_state["count"] = result["count"]
            _digest_state["error"] = None
        print(f"Digest ({reason}): {result['count']} SO(s) to {result['sent_to']}")
        tracking.record_job_run("daily_digest", "ok",
                                f"{result['count']} SO(s), {result.get('gaps', 0)} issue(s) [{reason}]")
        return True
    except Exception as exc:
        with _digest_lock:
            _digest_state["error"] = str(exc)
        print(f"WARNING: digest send failed ({reason}): {exc}")
        tracking.record_job_run("daily_digest", "error", f"{str(exc)[:180]} [{reason}]")
        return False
    finally:
        with _digest_lock:
            _digest_state["sending"] = False


def _run_daily_digest_job() -> None:
    """Scheduled SAFETY-NET for the accounting digest. The digest normally fires the
    moment a push completes (see _run_netsuite_push) -- no waiting for a fixed time.
    This daily run only sends when something is still unreported: newly-pushed SOs a
    post-push send missed (e.g. mail was down), or invoiced-but-no-SO gaps. Quiet days
    send nothing. Honors the auto-digest toggle."""
    if not _auto_digest_enabled():
        print("Digest: auto-send disabled via settings, skipping scheduled run")
        tracking.record_job_run("daily_digest", "skipped", "disabled")
        return
    try:
        data = _so_digest_data()
    except Exception as exc:
        print(f"WARNING: digest data build failed: {exc}")
        tracking.record_job_run("daily_digest", "error", str(exc)[:200])
        return
    if not data["new_sos"] and not data["gaps"]:
        tracking.record_job_run("daily_digest", "ok", "nothing to report")
        return
    _send_digest_safe("scheduled")


# ---- Automation control: on/off toggles, schedule, and run logs ----------
# Each scheduled job self-checks its toggle (persisted in the settings table)
# and records a run in job_runs, so the dashboard can show state + history.
AUTO_SYNC_SETTING = "auto_sync_enabled"
AUTO_NS_EXPORT_SETTING = "auto_ns_export_enabled"
AUTO_NS_PUSH_SETTING = "auto_ns_push_enabled"
# Order alerts were already live when they got their own job (2026-09-21), so the
# toggle defaults ON -- the deploy changes when they run, not whether they run.
AUTO_ALERTS_SETTING = "auto_alerts_enabled"

# Registry drives the /api/automation panel. `default` "false" means the job is
# off until someone turns it on (the NetSuite auto-push stays off until
# accounting signs off). Keep `id` in sync with the scheduler job ids below.
AUTOMATION_JOBS = [
    {"id": "daily_refresh", "label": "Invoice sync (Crstl)", "schedule": "Daily · 4:45 AM ET",   "setting": AUTO_SYNC_SETTING,    "default": "true"},
    {"id": "netsuite_push", "label": "NetSuite auto-push",   "schedule": "Mon–Fri · 5:00 AM ET", "setting": AUTO_NS_PUSH_SETTING, "default": "false"},
    {"id": "daily_digest",  "label": "Daily digest email",   "schedule": "Mon–Fri · 7:15 AM ET", "setting": AUTO_DIGEST_SETTING,  "default": "true"},
    {"id": "finale_push",   "label": "Finale invoicing",     "schedule": "Every 15 min",         "setting": AUTO_FINALE_SETTING,  "default": "false"},
    # Not its own scheduler job: it is the third pass of finale_push (runs_with),
    # so its next run is that job's, and it is silent whenever that job is off.
    {"id": "finale_dsd",    "label": "DSD pickup numbers → Finale (PRO / RTS)", "schedule": "Every 15 min · inside Finale invoicing",
     "setting": AUTO_DSD_SETTING, "default": "false", "runs_with": "finale_push"},
    {"id": "order_alerts",  "label": "Order alerts email",   "schedule": "Mon–Fri · every 15 min", "setting": AUTO_ALERTS_SETTING, "default": "true"},
]
_JOB_BY_ID = {j["id"]: j for j in AUTOMATION_JOBS}


def _job_enabled(setting: str, default: str = "true") -> bool:
    return (tracking.get_setting(setting, default) or default).lower() != "false"


def _run_refresh_job() -> None:
    if not _job_enabled(AUTO_SYNC_SETTING):
        tracking.record_job_run("daily_refresh", "skipped", "disabled"); return
    _refresh_cache()
    with _cache_lock:
        status, n = _cache.get("status", ""), len(_cache["invoices"])
    if str(status).startswith("error"):
        tracking.record_job_run("daily_refresh", "error", str(status)[:200])
    else:
        tracking.record_job_run("daily_refresh", "ok", f"{n} invoices synced")


def _run_ns_export_job() -> None:
    if not _job_enabled(AUTO_NS_EXPORT_SETTING):
        tracking.record_job_run("netsuite_export", "skipped", "disabled"); return
    try:
        _generate_netsuite_export()
        with _netsuite_lock:
            st = dict(_netsuite_state)
        if st.get("error"):
            tracking.record_job_run("netsuite_export", "error", str(st["error"])[:200])
        else:
            tracking.record_job_run("netsuite_export", "ok", f"{st.get('count', 0)} rows, {st.get('skipped', 0)} skipped")
    except Exception as exc:
        tracking.record_job_run("netsuite_export", "error", str(exc)[:200])


def _run_netsuite_push_job() -> None:
    """Scheduled live push (default OFF). Pushes only acknowledged, non-zero
    invoices not yet pushed (tracking dedup) so it never re-posts a manual push;
    idempotent upsert makes a retry safe. Records the run + updates push state.

    AUTOMATION-ONLY guards from config `automation` (select_for_automation), all
    keyed on created_at (when CRSTL created the record), not invoice_date: an
    absolute go-live FLOOR (nothing created before it is ever auto-pushed -- the
    149-incident backstop), a ROLLING created_within_days window (only recent
    inflow auto-pushes, so the old backlog can never be dredged up and the guard
    self-scales with volume), and a per-run hard CAP (an oversized set is REFUSED,
    not truncated -- review and run manually to override). Manual pushes from the
    app are NOT subject to any of these."""
    if not _job_enabled(AUTO_NS_PUSH_SETTING, default="false"):
        tracking.record_job_run("netsuite_push", "skipped", "disabled"); return
    auto = load_refs().get("automation") or {}
    cutoff = str(auto.get("go_live_after") or "") or None
    within = auto.get("created_within_days")
    cap = auto.get("max_per_run")
    with _cache_lock:
        invoices = list(_cache["invoices"])
    candidates = eligible_for_push(invoices)
    unpushed = tracking.get_unpushed_ids([str(i["transaction_id"]) for i in candidates])
    to_push, blocked = select_for_automation(candidates, unpushed,
                                              created_after=cutoff,
                                              created_within_days=within,
                                              max_per_run=cap)
    if blocked:
        tracking.record_job_run("netsuite_push", "blocked",
                                f"{blocked} -- refusing; run manually from the app to override")
        return
    if not to_push:
        window = f" (created on/after {cutoff}" if cutoff else ""
        if within is not None:
            window = f"{window or ' (created'}, within {within}d"
        window = f"{window})" if window else ""
        tracking.record_job_run("netsuite_push", "ok", f"nothing new to push{window}")
        return
    # _run_netsuite_push reads the cache, pushes just these ids, sanitizes, and
    # updates _netsuite_push_state (so the dashboard's last-push panel reflects it).
    result = _run_netsuite_push(True, [str(i["transaction_id"]) for i in to_push], None)
    s = result["summary"]
    if result.get("blocked"):
        tracking.record_job_run("netsuite_push", "blocked", "unresolved ids: " + ", ".join(result["unresolved"]))
    else:
        status = "ok" if s["failed"] == 0 else "partial"
        tracking.record_job_run("netsuite_push", status,
                                f"{s['sent']} sent, {s['failed']} failed, {s['skipped_no_map']} skipped")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    tracking.init_db()
    await asyncio.to_thread(_refresh_cache)

    # Set SCHEDULER_ENABLED=false on a dev workstation so a local `uvicorn --reload`
    # can't fire the daily digest / NetSuite export / Crstl refresh in parallel
    # with the production LXC. Running two schedulers against the same Crstl
    # tenant produced two digest emails at 07:15 and 07:19 with different
    # counts because each instance has its own tracking.db. Defaults to enabled
    # so the LXC just works after `systemctl restart`.
    if os.environ.get("SCHEDULER_ENABLED", "true").lower() in ("0", "false", "no"):
        print("Scheduler disabled via SCHEDULER_ENABLED — daily jobs will not run in this instance.")
        yield
        return

    # misfire_grace_time=3600 lets a job run up to 1 hour late if the host was
    # paused or the scheduler was down at fire time (LXC snapshots, restarts).
    # Without this, a missed 07:00 refresh silently vanishes until the next day.
    # Jobs run through toggle-aware wrappers (_run_*_job) that self-check their
    # on/off setting and record each run in job_runs, so the Automation panel can
    # show state + history. A disabled job still fires but no-ops and logs "skipped".
    global _scheduler
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_run_refresh_job, "cron", id="daily_refresh",
                       hour=4, minute=45, timezone="America/Toronto",
                       misfire_grace_time=3600, coalesce=True)
    # NetSuite auto-push — 5:00 AM ET, before anyone in accounting is entering
    # invoices, so our writes never collide with a manual entry. Runs after the
    # 4:45 refresh (fresh data) and before the 7:15 digest (which reports it).
    # OFF by default until accounting turns it on.
    _scheduler.add_job(_run_netsuite_push_job, "cron", id="netsuite_push",
                       day_of_week="mon-fri", hour=5, minute=0, timezone="America/Toronto",
                       misfire_grace_time=3600, coalesce=True)
    # Weekdays only — nobody works the digest queue on Sat/Sun, so a weekend
    # send is just two emails to ignore. Skipping them loses nothing: the digest
    # sends whatever tracking.db still has unemailed, so Monday 07:15 carries
    # Friday's late invoices plus anything Crstl added over the weekend.
    _scheduler.add_job(_run_daily_digest_job, "cron", id="daily_digest",
                       day_of_week="mon-fri", hour=7, minute=15,
                       timezone="America/Toronto",
                       misfire_grace_time=3600, coalesce=True)
    # Finale invoicing poll -- every 15 minutes. HD accepts the 810 a median 6 min after
    # the ship, so this lands the Finale invoice + order completion ~15-20 min after
    # shipping with exact 810 cents. Cheap when idle (one CRSTL list call). OFF unless
    # config finale.enabled AND the dashboard toggle are both on.
    _scheduler.add_job(_run_finale_push_job, "interval", id="finale_push", minutes=15,
                       misfire_grace_time=600, coalesce=True)
    # Order alerts -- every 15 minutes, its own job (see _run_alerts_job). Offset 7
    # minutes from the Finale poll so the two don't hit CRSTL and Finale at once.
    _scheduler.add_job(_run_alerts_job, "interval", id="order_alerts", minutes=15,
                       next_run_time=datetime.now(timezone.utc) + timedelta(minutes=7),
                       misfire_grace_time=600, coalesce=True)
    _scheduler.start()
    yield
    _scheduler.shutdown()


app = FastAPI(title="HD Decorating Invoice Dashboard", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict:
    """Lightweight liveness probe for container orchestrators. Does not touch
    the cache lock or the Crstl API. Includes tracking-DB write health so
    persistence failures surface before they produce duplicate digest emails."""
    return {"status": "ok", "tracking": tracking.write_health()}


@app.get("/api/invoices")
def get_invoices() -> dict:
    with _cache_lock:
        invoices = list(_cache["invoices"])
        last_synced = _cache["last_synced"]
        status = _cache["status"]

    if invoices:
        tx_ids = [inv["transaction_id"] for inv in invoices]
        events = tracking.get_latest_events(tx_ids)
        invoices = [
            {**inv, **events.get(inv["transaction_id"], {"exported_at": None, "netsuite_at": None}),
             "netsuite_customer": _netsuite_customer(inv)}
            for inv in invoices
        ]

    # The go-live cutoff (automation config) so the dashboard can hide the
    # pre-cutoff backlog by default -- those older invoices are handled and only
    # clutter the "not pushed" view.
    go_live_after = (load_refs().get("automation") or {}).get("go_live_after") or ""
    return {"invoices": invoices, "last_synced": last_synced, "status": status,
            "go_live_after": go_live_after}


def _netsuite_customer(inv: dict) -> dict | None:
    """The NetSuite customer this invoice would post to (name + id + channel), or
    None if it can't be routed. Uses the SAME resolver as the push, so the flyout
    shows exactly the dry-run/live target."""
    route = resolve_customer(inv, inv.get("province"), inv.get("store"))
    if not route or not route.get("customer_id"):
        return None
    return {"id": route["customer_id"], "name": route.get("customer_name"), "channel": route["channel"]}


@app.post("/api/sync")
def sync() -> dict:
    _refresh_cache()
    with _cache_lock:
        snapshot = {**_cache}
    return {"ok": snapshot["status"] == "ok", "last_synced": snapshot["last_synced"], "status": snapshot["status"]}


class ExportRequest(BaseModel):
    ids: Optional[list[str]] = None


@app.post("/api/export")
def export(body: ExportRequest = ExportRequest()) -> Response:
    with _cache_lock:
        invoices = list(_cache["invoices"])
    if not invoices:
        return JSONResponse(
            status_code=503,
            content={"message": "Cache is empty. Trigger /api/sync first."},
        )
    # A bulk export is a report, so it carries Accepted only. An explicit id
    # list is a deliberate pick from the dashboard and is honoured as given.
    if body.ids is None:
        invoices = _reportable(invoices)
    else:
        wanted = set(body.ids)
        invoices = [inv for inv in invoices if inv["transaction_id"] in wanted]
    if not invoices:
        return JSONResponse(
            status_code=404,
            content={"message": "No invoices matched the request."},
        )

    try:
        xlsx_bytes = _workbook_for(invoices)
    except ReportUnavailable as exc:
        return JSONResponse(status_code=502, content={"message": str(exc)})

    tracking.record_events([inv["transaction_id"] for inv in invoices], "exported")

    filename = f"invoices_{date.today().isoformat()}.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class NetsuitePushRequest(BaseModel):
    # Dry run by default: a live send must be asked for explicitly. `ids` pushes
    # just those transaction_ids (the "test one invoice" flow); `limit` caps the
    # batch. There is no sandbox, so the safe path is dry_run -> ids -> live.
    # limit must be >= 1 when given: `limit=0` used to fall through to "no cap".
    dry_run: bool = True
    ids: Optional[list[str]] = None
    limit: Optional[int] = Field(default=None, ge=1)
    # Off by default: a record already in NetSuite is skipped, never overwritten.
    # Set true to explicitly UPDATE existing records (the "confirm to update" flow).
    confirm_existing: bool = False


def _sanitize_push_result(result: dict) -> dict:
    """Strip internal detail (raw NetSuite response bodies, exception text) from a
    push result before it is returned to / stored for the unauthenticated dashboard
    endpoints. Full detail goes to journald instead. Reconciliation figures stay --
    they are the dashboard's purpose and already sit behind the tailnet."""
    for row in result.get("results", []):
        if row.get("error"):
            print(f"NetSuite push error [{row.get('transaction_id')}]: {row['error']}")
            row["error"] = "upsert failed — see server logs"
    return result


def _run_netsuite_push(live: bool, ids: Optional[list[str]], limit: Optional[int],
                       confirm_existing: bool = False) -> dict:
    """Push the cached invoices via the shared engine and record the run. Runs in
    a worker thread (the upsert loop is blocking network I/O). confirm_existing=False
    (default, incl. the scheduled job) skips records already in NetSuite; True updates
    them (the dashboard's explicit 'confirm to update')."""
    with _cache_lock:
        invoices = list(_cache["invoices"])
    # Eligibility (Accepted + latest-per-invoice + non-zero) is enforced INSIDE
    # push_invoices, so the manual button, dry-run, scheduled job and CLI all get
    # the same filter -- no caller can bypass it.
    result = _sanitize_push_result(push_invoices(invoices, live=live, only=ids, limit=limit,
                                                 confirm_existing=confirm_existing))
    with _netsuite_push_lock:
        _netsuite_push_state.update({
            "last_run": datetime.now(timezone.utc).isoformat(),
            "mode": result["mode"],
            "summary": result["summary"],
            "unresolved": result["unresolved"],
            "results": result["results"],
            "blocked": result.get("blocked"),
            "error": None,
        })
    # Finale invoicing rides the same event: the invoices that just landed as SOs.
    if live and _finale_enabled():
        sent_ids = [str(r.get("transaction_id")) for r in (result.get("results") or [])
                    if r.get("status") == "sent"]
        if sent_ids:
            _run_finale_push_safe(sent_ids)
    # Fire the accounting digest the moment SOs actually land in NetSuite -- no
    # waiting for the scheduled run. Live pushes only; best-effort so a digest
    # failure never fails the push (the scheduled safety-net will catch it).
    if live and (result.get("summary") or {}).get("sent", 0) > 0 and _auto_digest_enabled():
        _send_digest_safe("post-push")
    return result


@app.post("/api/netsuite")
async def netsuite_push(body: NetsuitePushRequest = NetsuitePushRequest()) -> JSONResponse:
    """Manual push of Crstl invoices into NetSuite as invoices (TBA REST).
    Dry run unless dry_run=false. A live send is refused (nothing written) while
    any item/tax id is unresolved in config -- the unresolved list comes back so
    it can be filled first."""
    # A live send over this HTTP surface (the app has no auth of its own) MUST be
    # scoped to named invoices. Blocks the "one unauthenticated POST pushes every
    # invoice live to production" path; the operator CLI on the box can still do a
    # deliberate full-batch live send.
    if not body.dry_run and not body.ids:
        return JSONResponse(status_code=400, content={
            "message": "A live push must name the invoices to send (ids). "
                       "Use dry_run for an unscoped preview."})
    with _netsuite_push_lock:
        if _netsuite_push_state.get("running"):
            return JSONResponse(status_code=409, content={"message": "A NetSuite push is already in progress"})
        _netsuite_push_state["running"] = True
    try:
        result = await asyncio.to_thread(_run_netsuite_push, not body.dry_run, body.ids, body.limit,
                                         body.confirm_existing)
    except Exception as exc:
        print(f"NetSuite push failed: {exc}")   # detail to journald, not to the caller
        with _netsuite_push_lock:
            _netsuite_push_state["error"] = "push failed — see server logs"
        return JSONResponse(status_code=500, content={"message": "NetSuite push failed — see server logs"})
    finally:
        with _netsuite_push_lock:
            _netsuite_push_state["running"] = False
    with _netsuite_push_lock:
        last_run = _netsuite_push_state["last_run"]
    # A live send blocked on unresolved ids wrote nothing -> surface as 400.
    status_code = 400 if result.get("blocked") else 200
    return JSONResponse(status_code=status_code, content={**result, "last_run": last_run})


class FinalePushRequest(BaseModel):
    dry_run: bool = True
    ids: Optional[list[str]] = None
    limit: Optional[int] = Field(default=None, ge=1)


@app.post("/api/finale")
async def finale_push(body: FinalePushRequest = FinalePushRequest()) -> JSONResponse:
    """Manual Finale invoicing. Dry run unless dry_run=false; a live run must name
    the invoices (ids) -- same guard as /api/netsuite -- and is refused while any
    promo/tax-rate id is unresolved in config."""
    if not body.dry_run and not body.ids:
        return JSONResponse(status_code=400, content={
            "message": "A live Finale run must name the invoices to create (ids). Use dry_run for a preview."})
    try:
        result = await asyncio.to_thread(_run_finale_push, not body.dry_run, body.ids, body.limit)
    except FinaleBusy as exc:
        return JSONResponse(status_code=409, content={"message": str(exc)})
    except Exception as exc:
        print(f"Finale push failed: {exc}")
        with _finale_push_lock:
            _finale_push_state["error"] = "push failed — see server logs"
        return JSONResponse(status_code=500, content={"message": "Finale push failed — see server logs"})
    with _finale_push_lock:
        last_run = _finale_push_state["last_run"]
    return JSONResponse(status_code=400 if result.get("blocked") else 200, content={**result, "last_run": last_run})


@app.post("/api/finale/nonedi")
async def finale_nonedi_push(body: FinalePushRequest = FinalePushRequest()) -> JSONResponse:
    """Manual non-EDI Finale invoicing (HD Supply, Special Orders, ...). Dry run unless
    dry_run=false; a live run must name the Finale order ids."""
    if not body.dry_run and not body.ids:
        return JSONResponse(status_code=400, content={
            "message": "A live non-EDI run must name the Finale order ids (ids). Use dry_run for a preview."})
    try:
        result = await asyncio.to_thread(_run_nonedi_push, not body.dry_run, body.ids, body.limit)
    except FinaleBusy as exc:
        return JSONResponse(status_code=409, content={"message": str(exc)})
    except Exception as exc:
        print(f"non-EDI Finale push failed: {exc}")
        return JSONResponse(status_code=500, content={"message": "non-EDI Finale push failed — see server logs"})
    return JSONResponse(status_code=400 if result.get("blocked") else 200, content=result)


@app.post("/api/finale/dsd")
async def finale_dsd_prefill(body: FinalePushRequest = FinalePushRequest()) -> JSONResponse:
    """Manual DSD pre-fill (PRO/RTS from Accepted 856s onto open Finale shipments). Dry
    run unless dry_run=false; a live run must name the ASN ids."""
    if not body.dry_run and not body.ids:
        return JSONResponse(status_code=400, content={
            "message": "A live DSD run must name the ASN ids (ids). Use dry_run for a preview."})
    try:
        result = await asyncio.to_thread(_run_dsd_prefill, not body.dry_run, body.ids, body.limit)
    except FinaleBusy as exc:
        return JSONResponse(status_code=409, content={"message": str(exc)})
    except Exception as exc:
        print(f"DSD prefill failed: {exc}")
        return JSONResponse(status_code=500, content={"message": "DSD prefill failed — see server logs"})
    return JSONResponse(status_code=400 if result.get("blocked") else 200, content=result)


@app.post("/api/shipstation/close")
async def shipstation_close(body: FinalePushRequest = FinalePushRequest()) -> JSONResponse:
    """Mark DSD ShipStation orders shipped once Finale has shipped them. Dry by
    default; a live run must name order numbers (ids) like every other writer."""
    if not body.dry_run and not body.ids:
        return JSONResponse(status_code=400, content={"message": "live run requires ids (ShipStation order numbers)"})
    try:
        result = _run_shipstation_close(not body.dry_run, body.ids, body.limit)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"message": str(exc)[:200]})
    return JSONResponse(content=result)


@app.post("/api/dropship/prefill")
async def dropship_prefill(body: FinalePushRequest = FinalePushRequest()) -> JSONResponse:
    """Write carrier + tracking onto packed dropship shipments. Dry by default; a
    live run must name PO numbers, like every other writer."""
    if not body.dry_run and not body.ids:
        return JSONResponse(status_code=400, content={"message": "live run requires ids (PO numbers)"})
    try:
        result = _run_dropship_prefill(not body.dry_run, body.ids, body.limit)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"message": str(exc)[:200]})
    return JSONResponse(content=result)


@app.post("/api/alerts/check")
async def alerts_check(body: FinalePushRequest = FinalePushRequest()) -> JSONResponse:
    """Find order outliers; dry (default) previews the email, live sends it."""
    try:
        result = _run_alerts(not body.dry_run)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"message": str(exc)[:200]})
    return JSONResponse(content=result)


@app.get("/api/finale-push/latest")
async def finale_push_latest() -> JSONResponse:
    with _finale_push_lock:
        return JSONResponse(content=dict(_finale_push_state))


@app.get("/api/netsuite-push/latest")
def netsuite_push_latest() -> dict:
    with _netsuite_push_lock:
        return {**_netsuite_push_state}


@app.get("/api/netsuite-export/latest")
def netsuite_export_latest() -> dict:
    with _netsuite_lock:
        state = {**_netsuite_state}
    path = state.get("path")
    state["available"] = bool(path and pathlib.Path(path).exists())
    return state


@app.get("/api/netsuite-export/download")
def netsuite_export_download() -> Response:
    with _netsuite_lock:
        path = _netsuite_state.get("path")
    if not path or not pathlib.Path(path).exists():
        return JSONResponse(
            status_code=404,
            content={"message": "No NetSuite export file available. Generate one first."},
        )
    filename = pathlib.Path(path).name
    return Response(
        content=pathlib.Path(path).read_bytes(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/netsuite-export/generate")
async def netsuite_export_generate() -> dict:
    with _netsuite_lock:
        if _netsuite_state.get("generating"):
            return JSONResponse(status_code=409, content={"message": "Export already in progress"})
        _netsuite_state["generating"] = True
    try:
        await asyncio.to_thread(_generate_netsuite_export)
    except Exception as exc:
        return JSONResponse(status_code=500, content={"message": str(exc)})
    finally:
        with _netsuite_lock:
            _netsuite_state["generating"] = False
    with _netsuite_lock:
        state = {**_netsuite_state}
    path = state.get("path")
    state["available"] = bool(path and pathlib.Path(path).exists())
    return state


class EmailRequest(BaseModel):
    ids: Optional[list[str]] = None


@app.post("/api/email/send-digest")
async def send_digest_now(body: EmailRequest = EmailRequest()) -> JSONResponse:
    """Send an email of invoices. Without `ids`: unemailed digest (same as
    scheduled job). With `ids`: send exactly those invoices."""
    if body.ids is not None and not body.ids:
        return JSONResponse(status_code=400, content={"message": "ids is empty"})
    try:
        result = await asyncio.to_thread(_send_daily_digest, body.ids)
    except MailConfigError as exc:
        return JSONResponse(status_code=400, content={"message": str(exc)})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"message": str(exc)})
    return JSONResponse(result)


@app.post("/api/email/mark-all-emailed")
def mark_all_emailed() -> dict:
    """Baseline-reset the digest: mark every currently-cached invoice as
    already emailed WITHOUT sending anything. Use after a dev/prod tracking
    DB split or when you want to reset the "unemailed" state. Tomorrow's
    scheduled digest will only pick up invoices Crstl adds after this call."""
    with _cache_lock:
        tx_ids = [inv["transaction_id"] for inv in _cache["invoices"] if inv.get("transaction_id")]
    if not tx_ids:
        return {"marked": 0, "message": "cache is empty"}
    # Only mark ones not already marked, so we don't inflate the event log
    unemailed = tracking.get_unemailed_ids(tx_ids)
    tracking.record_events(unemailed, "emailed")
    return {"marked": len(unemailed), "already_emailed": len(tx_ids) - len(unemailed), "total_cached": len(tx_ids)}


@app.get("/api/email/status")
def email_status() -> dict:
    with _digest_lock:
        state = {**_digest_state}
    # Fall back to the tracking DB after a restart wipes in-memory state.
    # Only surfaces sends that actually marked invoices — a 0-count heartbeat
    # right before restart won't be recoverable.
    if state.get("last_sent") is None:
        state["last_sent"] = tracking.latest_event_time("emailed")
    state["auto_enabled"] = _auto_digest_enabled()
    return state


class AutoDigestToggle(BaseModel):
    enabled: bool


@app.post("/api/email/auto-digest")
def set_auto_digest(body: AutoDigestToggle) -> dict:
    """Enable or disable the scheduled weekday digest (Mon–Fri 07:15 Toronto).
    Persisted in tracking.db so the setting survives restarts. Manual sends are
    always available, including on weekends."""
    tracking.set_setting(AUTO_DIGEST_SETTING, "true" if body.enabled else "false")
    return {"auto_enabled": body.enabled}


@app.get("/api/automation")
def automation_status() -> dict:
    """The scheduled jobs with their on/off state, schedule, next run, and last
    run — drives the Automation panel."""
    next_runs = {}
    if _scheduler is not None:
        for j in _scheduler.get_jobs():
            nrt = getattr(j, "next_run_time", None)
            next_runs[j.id] = nrt.isoformat() if nrt else None
    last = {}
    for run in tracking.recent_job_runs(300):
        last.setdefault(run["job"], run)  # first seen = most recent (DESC order)
    jobs = [{
        "id": j["id"], "label": j["label"], "schedule": j["schedule"],
        "enabled": _job_enabled(j["setting"], j["default"]),
        "next_run": next_runs.get(j.get("runs_with") or j["id"]),
        "last_run": last.get(j["id"]),
    } for j in AUTOMATION_JOBS]
    return {"jobs": jobs, "scheduler_running": _scheduler is not None}


class AutomationToggle(BaseModel):
    job: str
    enabled: bool


@app.post("/api/automation")
def automation_toggle(body: AutomationToggle) -> JSONResponse:
    """Turn a scheduled job on or off. Persisted in tracking.db (survives
    restarts). A disabled job still fires on schedule but no-ops and logs it."""
    job = _JOB_BY_ID.get(body.job)
    if not job:
        return JSONResponse(status_code=404, content={"message": f"unknown job {body.job!r}"})
    tracking.set_setting(job["setting"], "true" if body.enabled else "false")
    return JSONResponse(content={"job": body.job, "enabled": body.enabled})


@app.get("/api/automation/logs")
def automation_logs(limit: int = 50) -> dict:
    """Recent scheduled-job runs, newest first (durable — from job_runs)."""
    return {"runs": tracking.recent_job_runs(min(max(limit, 1), 200))}


app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
