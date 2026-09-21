"""The in-process CRSTL cache: every 810 (with its 850 province/store/product) and
the 850 map. Built by the full refresh (startup + 4:45 daily) and topped up by the
incremental refresh the 15-min Finale poll runs. Each process that needs CRSTL data
(the web app for reports/NetSuite, the Finale worker for invoicing) holds its own."""
import os
import threading
from datetime import date, datetime, timedelta, timezone

from app.crstl import CrstlClient
from app import tracking
from app.report import flavor_of, product_for
from app.finale import FinaleClient
from app.shipments import merge_asn_dates, merge_finale_ship_dates
from app.automation import AUTO_SYNC_SETTING
from app import automation


# po_provinces is kept, not just applied to the invoices, because the workbook
# recovers a Dropship invoice's province from its 850 the same way -- and
# rebuilding that map at export time would crawl every 850 on record again.
_cache: dict = {"invoices": [], "last_synced": None, "status": "never", "po_provinces": {}}
_cache_lock = threading.Lock()


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


def _crstl_po_set() -> set:
    """Every PO number Crstl knows -- the 810s in the cache and the 850 map. A Finale
    sale order whose id is NOT in here is non-EDI (classification by exclusion)."""
    with _cache_lock:
        pos = {str(i.get("po_number") or "") for i in _cache["invoices"]}
        pos |= set(_cache["po_provinces"].keys())
    pos.discard("")
    return pos


def _run_refresh_job() -> None:
    if not automation._job_enabled(AUTO_SYNC_SETTING):
        tracking.record_job_run("daily_refresh", "skipped", "disabled"); return
    _refresh_cache()
    with _cache_lock:
        status, n = _cache.get("status", ""), len(_cache["invoices"])
    if str(status).startswith("error"):
        tracking.record_job_run("daily_refresh", "error", str(status)[:200])
    else:
        tracking.record_job_run("daily_refresh", "ok", f"{n} invoices synced")
