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
from pydantic import BaseModel

from app.crstl import CrstlClient
from app import tracking
from app.mail import send_mail, MailConfigError
from app.netsuite import transform_invoice
from app.netsuite_csv import build_netsuite_csv
from app.report import (XLSX_MEDIA_TYPE, flavor_of, rows_for_transactions,
                        window_label, workbook_bytes)


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
_digest_state: dict = {"last_sent": None, "count": 0, "error": None, "sending": False}
_digest_lock = threading.Lock()


def _build_mock_po_provinces() -> dict[str, dict]:
    provinces = ["ON", "BC", "QC", "AB", "SK", "MB", "NS", "NB", "NL", "PE", "NT", "YT", "NU"]
    result = {}
    for i in range(50):
        po = f"PO-{98801 - i * 3}"
        if i % 10 < 2:
            result[po] = {"province": "ON", "store": "VAUGHAN"}
        elif i % 10 < 4:
            result[po] = {"province": "AB", "store": "CALGARY"}
        else:
            result[po] = {"province": provinces[i % len(provinces)], "store": None}
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


def _attach_provinces(invoices: list[dict], po_provinces: dict[str, dict]) -> None:
    """Attach ship-to province + store from the source 850 to each invoice (factual, no rate math)."""
    for inv in invoices:
        loc = po_provinces.get(inv.get("po_number", ""), {})
        inv["province"] = loc.get("province")
        inv["store"] = loc.get("store")


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
        "province": inv.get("province") or "",
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
        line_items = transform_invoice(inv, province=inv.get("province"), store=inv.get("store"))
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
# known Draft. Only "Draft" and "Accepted" have ever been observed.
REPORTABLE_STATUSES = frozenset({"Accepted"})
_KNOWN_UNREPORTABLE = frozenset({"Draft"})


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


def _send_daily_digest(selected_ids: list[str] | None = None) -> dict:
    """Send an invoice email.

    Two modes:
    - Digest (selected_ids is None): pull invoices not yet emailed. Always
      sends, even on zero new invoices — accounting uses receipt as proof
      the pipeline is alive.
    - Selection (selected_ids provided): send exactly those invoices,
      regardless of whether they've been emailed before.

    In both modes, sent invoices are marked with an 'emailed' tracking event
    so they don't appear in future digests.

    Returns a summary dict. Raises MailConfigError if env vars missing.
    """
    recipients_raw = os.environ.get("MAIL_RECIPIENTS", "")
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    if not recipients:
        raise MailConfigError("MAIL_RECIPIENTS not set (comma-separated addresses)")

    with _cache_lock:
        invoices = list(_cache["invoices"])

    if selected_ids is not None:
        selected_set = set(selected_ids)
        to_send = [inv for inv in invoices if inv["transaction_id"] in selected_set]
        mode = "selection"
    else:
        # Accepted only. Drafts are the warehouse's superseded resubmissions;
        # emailing them duplicates invoices in accounting's queue.
        reportable = _reportable(invoices)
        all_ids = [inv["transaction_id"] for inv in reportable]
        new_ids = set(tracking.get_unemailed_ids(all_ids))
        to_send = [inv for inv in reportable if inv["transaction_id"] in new_ids]
        mode = "digest"

    today = date.today().isoformat()
    if to_send:
        total = sum(inv.get("total_amount", 0) for inv in to_send)
        by_province: dict[str, int] = {}
        for inv in to_send:
            p = inv.get("province") or "—"
            by_province[p] = by_province.get(p, 0) + 1
        # HTML-escape every interpolated value — province today is a 2-letter code from Crstl
        # so exploitability is nil, but Crstl-controlled strings drifting into email HTML is
        # the exact class of injection that becomes a real bug the day they add a description field.
        prov_rows = "".join(
            f"<tr><td>{html.escape(p)}</td><td style='text-align:right'>{c}</td></tr>"
            for p, c in sorted(by_province.items())
        )

        if mode == "selection":
            subject = f"HD Invoices — {today} — {len(to_send)} selected"
            intro = f"<p>{len(to_send)} invoice(s) sent manually from the dashboard.</p>"
        else:
            subject = f"HD Invoice Digest — {today} — {len(to_send)} new"
            intro = f"<p>{len(to_send)} new invoice(s) since the last digest.</p>"

        body_html = f"""
            {intro}
            <p><strong>Total value:</strong> ${total:,.2f} CAD</p>
            <table cellpadding="4" cellspacing="0" border="1" style="border-collapse:collapse">
              <tr><th align="left">Province</th><th align="right">Count</th></tr>
              {prov_rows}
            </table>
            <p>Full details attached as an Excel workbook.</p>
        """
        # Raises ReportUnavailable if Crstl can't be reached. That aborts the
        # send, which is deliberate: nothing is marked emailed, so tomorrow's
        # digest carries these invoices instead of accounting receiving a
        # workbook that quietly omits them.
        attachments = [(f"hd_invoices_{today}.xlsx", _workbook_for(to_send), XLSX_MEDIA_TYPE)]
    else:
        # Only reachable in digest mode — selection mode with 0 matches is a caller bug
        subject = f"HD Invoice Digest — {today} — 0 new"
        body_html = "<p>No new invoices since the last digest. Pipeline is healthy.</p>"
        attachments = None

    send_mail(subject=subject, body_html=body_html, recipients=recipients, attachments=attachments)

    if to_send:
        tracking.record_events([inv["transaction_id"] for inv in to_send], "emailed")

    return {"sent_to": recipients, "count": len(to_send), "subject": subject, "mode": mode}


AUTO_DIGEST_SETTING = "auto_digest_enabled"


def _auto_digest_enabled() -> bool:
    """Runtime toggle read from the settings table. Defaults to enabled if the
    setting has never been set — production LXC just works after deploy."""
    return tracking.get_setting(AUTO_DIGEST_SETTING, "true").lower() != "false"


def _run_daily_digest_job() -> None:
    """Scheduler entry point — wraps _send_daily_digest with state tracking.
    Honors the runtime auto-digest toggle: if disabled, the job logs and
    exits without sending. Manual /api/email/send-digest is unaffected."""
    if not _auto_digest_enabled():
        print("Digest: auto-send disabled via settings, skipping scheduled run")
        return
    with _digest_lock:
        if _digest_state.get("sending"):
            print("Digest: already running, skipping")
            return
        _digest_state["sending"] = True
    try:
        result = _send_daily_digest()
        with _digest_lock:
            _digest_state["last_sent"] = datetime.now(timezone.utc).isoformat()
            _digest_state["count"] = result["count"]
            _digest_state["error"] = None
        print(f"Digest: sent {result['count']} invoices to {result['sent_to']}")
    except Exception as exc:
        with _digest_lock:
            _digest_state["error"] = str(exc)
        print(f"WARNING: digest send failed: {exc}")
    finally:
        with _digest_lock:
            _digest_state["sending"] = False


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
    scheduler = AsyncIOScheduler()
    scheduler.add_job(_refresh_cache, "cron", id="daily_refresh",
                      hour=7, minute=0, timezone="America/Toronto",
                      misfire_grace_time=3600, coalesce=True)
    scheduler.add_job(_generate_netsuite_export, "cron", id="netsuite_export",
                      hour=4, minute=0, timezone="America/Toronto",
                      misfire_grace_time=3600, coalesce=True)
    # Weekdays only — nobody works the digest queue on Sat/Sun, so a weekend
    # send is just two emails to ignore. Skipping them loses nothing: the digest
    # sends whatever tracking.db still has unemailed, so Monday 07:15 carries
    # Friday's late invoices plus anything Crstl added over the weekend.
    scheduler.add_job(_run_daily_digest_job, "cron", id="daily_digest",
                      day_of_week="mon-fri", hour=7, minute=15,
                      timezone="America/Toronto",
                      misfire_grace_time=3600, coalesce=True)
    scheduler.start()
    yield
    scheduler.shutdown()


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
            {**inv, **events.get(inv["transaction_id"], {"exported_at": None, "netsuite_at": None})}
            for inv in invoices
        ]

    return {"invoices": invoices, "last_synced": last_synced, "status": status}


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


@app.post("/api/netsuite")
def netsuite() -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={"message": "NetSuite REST connector not yet configured. Use CSV export path."},
    )


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


app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
