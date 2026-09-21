"""Accounting: the CRSTL invoice export workbook, the NetSuite CSV + REST push, and
the accounting digest email. Runs in the web app (crstl-api)."""
import html
import os
import pathlib
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app import tracking
from app.mail import send_mail, MailConfigError
from app.netsuite import transform_invoice, resolve_customer, external_id_for
from app.netsuite_csv import build_netsuite_csv
from app.netsuite_push import push_invoices, eligible_for_push, select_for_automation
from app.netsuite_payload import load_refs
from app.report import (
    XLSX_MEDIA_TYPE, dates_for, flavor_of, product_for, rows_for_transactions, window_label,
    workbook_bytes)
from app.automation import AUTO_DIGEST_SETTING, AUTO_NS_EXPORT_SETTING, AUTO_NS_PUSH_SETTING
from app.crstl_cache import _MOCK_PO_PROVINCES, _cache, _cache_lock
from app.finale_jobs import _EMPTY_RECON
from app import automation, crstl_cache, finale_jobs


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
    if crstl_cache._mock_mode():
        rows = sorted((_mock_report_row(inv) for inv in invoices),
                      key=lambda r: (r["flavor"], r["invoice"]))
    else:
        with _cache_lock:
            po_index = dict(_cache["po_provinces"])
        ids = [inv["transaction_id"] for inv in invoices if inv.get("transaction_id")]
        rows = rows_for_transactions(crstl_cache._get_client(), ids, po_index)
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
    fin_cfg = finale_jobs._finale_config()
    # Non-EDI receipts since the last digest that was actually SENT (a digest fires
    # after each push as well as at 7:15), so a figure is never re-reported as new.
    since = _last_digest_sent_at() or (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    nonedi = [r for r in tracking.recent_finale_invoices(since) if str(r.get("key", "")).startswith("order:")]
    # The reconciliation runs FIRST: it may refresh a draft receipt that has since
    # been posted by hand, and the headline must show that state.
    recon = finale_jobs._finale_reconciliation(invoices, fin_cfg.get("stale_pickup_days")) if finale_jobs._finale_enabled() else _EMPTY_RECON
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
            "finale": finale, "finale_enabled": finale_jobs._finale_enabled(),
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


def _run_ns_export_job() -> None:
    if not automation._job_enabled(AUTO_NS_EXPORT_SETTING):
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
    if not automation._job_enabled(AUTO_NS_PUSH_SETTING, default="false"):
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


def _netsuite_customer(inv: dict) -> dict | None:
    """The NetSuite customer this invoice would post to (name + id + channel), or
    None if it can't be routed. Uses the SAME resolver as the push, so the flyout
    shows exactly the dry-run/live target."""
    route = resolve_customer(inv, inv.get("province"), inv.get("store"))
    if not route or not route.get("customer_id"):
        return None
    return {"id": route["customer_id"], "name": route.get("customer_name"), "channel": route["channel"]}


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
    if live and finale_jobs._finale_enabled():
        sent_ids = [str(r.get("transaction_id")) for r in (result.get("results") or [])
                    if r.get("status") == "sent"]
        if sent_ids:
            finale_jobs._run_finale_push_safe(sent_ids)
    # Fire the accounting digest the moment SOs actually land in NetSuite -- no
    # waiting for the scheduled run. Live pushes only; best-effort so a digest
    # failure never fails the push (the scheduled safety-net will catch it).
    if live and (result.get("summary") or {}).get("sent", 0) > 0 and _auto_digest_enabled():
        _send_digest_safe("post-push")
    return result
