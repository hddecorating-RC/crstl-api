import contextlib
import fcntl
import json
import os
import pathlib
import sqlite3
from datetime import datetime, timezone

# Event types the app writes. Enforced in application code, not via a DB CHECK
# constraint — SQLite can't ALTER a CHECK, and letting the schema outlive the
# app's event vocabulary made adding 'emailed' painful.
EVENT_TYPES = ("exported", "netsuite", "emailed", "so_digest", "finale")

# Last write failure — surfaced via `write_health()` so the /api/health endpoint
# can report "digest ran but couldn't record — expect re-sends tomorrow".
_last_write_error: str | None = None
_last_write_error_at: str | None = None


def _db_path() -> str:
    return os.environ.get("TRACKING_DB", ".tmp/tracking.db")


def init_db() -> None:
    path = _db_path()
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    # One process at a time: the web app and both workers start together, and two
    # first-time inits race ("database is locked" on the WAL switch; both would
    # CREATE invoice_events). Blocking -- the other process's init takes milliseconds.
    with open(pathlib.Path(path).parent / "tracking-init.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with contextlib.closing(_connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            _create_or_migrate(conn)


def _create_or_migrate(conn: sqlite3.Connection) -> None:
    """Create the table on a fresh DB, or migrate an older schema that has a
    restrictive CHECK constraint blocking newer event types."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='invoice_events'"
    ).fetchone()

    # Always ensure the settings table exists — used for runtime toggles like
    # auto-digest enable/disable. Cheap CREATE IF NOT EXISTS on every start.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS job_runs (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            job     TEXT NOT NULL,
            status  TEXT NOT NULL,
            detail  TEXT,
            ran_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_job_runs_ran_at ON job_runs(ran_at);
        CREATE TABLE IF NOT EXISTS netsuite_records (
            external_id   TEXT PRIMARY KEY,
            netsuite_id   TEXT,
            last_modified TEXT,
            updated_at    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS finale_invoices (
            transaction_id  TEXT PRIMARY KEY,
            po_number       TEXT,
            invoice_id      TEXT,
            invoice_url     TEXT,
            invoice_id_user TEXT,
            status          TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            created_by      TEXT,
            finale_total    REAL,
            delta           REAL
        );
        CREATE TABLE IF NOT EXISTS alerts (
            key         TEXT PRIMARY KEY,
            po_number   TEXT,
            issue       TEXT NOT NULL,
            sent_at     TEXT NOT NULL,
            resolved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS push_snapshots (
            key            TEXT PRIMARY KEY,
            transaction_id TEXT NOT NULL,
            invoice_number TEXT,
            target         TEXT NOT NULL,
            hd_total       REAL,
            recorded_at    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invoice_checks (
            key            TEXT PRIMARY KEY,
            invoice_number TEXT NOT NULL,
            issue          TEXT NOT NULL,
            problem        TEXT,
            first_seen     TEXT NOT NULL,
            resolved_at    TEXT
        );
        CREATE TABLE IF NOT EXISTS dropship_marks (
            shipment_url TEXT PRIMARY KEY,
            po_number    TEXT,
            tracking     TEXT NOT NULL,
            carrier_url  TEXT NOT NULL,
            marked_at    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shipstation_marks (
            order_id     TEXT PRIMARY KEY,
            order_number TEXT,
            tracking     TEXT,
            ship_date    TEXT,
            status       TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS finale_shipments (
            asn_id          TEXT PRIMARY KEY,
            po_number       TEXT,
            shipment_id     TEXT,
            pro             TEXT,
            rts             TEXT,
            status          TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );
    """)
    # Reconciliation columns added 2026-09-16 (who made the Finale invoice, its total,
    # the delta vs the 810): a DB from before then has the table without them.
    have = {r[1] for r in conn.execute("PRAGMA table_info(finale_invoices)")}
    for col, typ in (("created_by", "TEXT"), ("finale_total", "REAL"), ("delta", "REAL")):
        if col not in have:
            conn.execute(f"ALTER TABLE finale_invoices ADD COLUMN {col} {typ}")
    conn.commit()

    if row is None:
        conn.executescript("""
            CREATE TABLE invoice_events (
                transaction_id TEXT NOT NULL,
                event_type     TEXT NOT NULL,
                occurred_at    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_invoice_events_tx
                ON invoice_events(transaction_id);
        """)
        return

    existing_sql = row[0] or ""
    # Old schema had CHECK(event_type IN ('exported', 'netsuite')) — needs to go.
    if "CHECK" in existing_sql and "'emailed'" not in existing_sql:
        with conn:
            conn.executescript("""
                CREATE TABLE invoice_events_new (
                    transaction_id TEXT NOT NULL,
                    event_type     TEXT NOT NULL,
                    occurred_at    TEXT NOT NULL
                );
                INSERT INTO invoice_events_new (transaction_id, event_type, occurred_at)
                    SELECT transaction_id, event_type, occurred_at FROM invoice_events;
                DROP TABLE invoice_events;
                ALTER TABLE invoice_events_new RENAME TO invoice_events;
                CREATE INDEX IF NOT EXISTS idx_invoice_events_tx
                    ON invoice_events(transaction_id);
            """)


def _connect() -> sqlite3.Connection:
    # Three processes write here (web app, Finale worker, order-watch); wait up to
    # 15 s for another's write to finish rather than sqlite's default 5.
    return sqlite3.connect(_db_path(), check_same_thread=False, timeout=15)


def record_events(transaction_ids: list[str], event_type: str) -> None:
    global _last_write_error, _last_write_error_at
    if not transaction_ids:
        return
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event_type {event_type!r}; expected one of {EVENT_TYPES}")
    now = datetime.now(timezone.utc).isoformat()
    rows = [(tx_id, event_type, now) for tx_id in transaction_ids]
    try:
        with contextlib.closing(_connect()) as conn:
            with conn:
                conn.executemany(
                    "INSERT INTO invoice_events (transaction_id, event_type, occurred_at) VALUES (?, ?, ?)",
                    rows,
                )
        _last_write_error = None
        _last_write_error_at = None
    except Exception as exc:
        # Best-effort: don't raise into callers (CSV export must still return the file,
        # digest email must still be recorded as sent). But make the failure loud in
        # journalctl AND surface it via write_health() so /api/health can report it.
        _last_write_error = f"{event_type}: {exc}"
        _last_write_error_at = now
        print(f"ERROR: tracking write failed ({event_type}, {len(transaction_ids)} rows): {exc}")


def get_setting(key: str, default: str | None = None) -> str | None:
    """Read a setting value. Returns default if the key is missing or the read fails."""
    try:
        with contextlib.closing(_connect()) as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default
    except Exception as exc:
        print(f"WARNING: settings read failed for {key!r}: {exc}")
        return default


def set_setting(key: str, value: str) -> None:
    """Upsert a setting value. Best-effort — errors are logged but not raised
    (callers are UI toggles; a persistence failure shouldn't 500 the request)."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with contextlib.closing(_connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (key, value, now),
                )
    except Exception as exc:
        print(f"ERROR: settings write failed for {key!r}: {exc}")


# State one service writes and another reads, e.g. the Finale worker's last-run
# results that the web app's dashboard shows. JSON in the settings table; the same
# best-effort contract as get_setting / set_setting.
def set_json(key: str, value) -> None:
    set_setting(key, json.dumps(value, default=str))


def get_json(key: str, default=None):
    raw = get_setting(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def write_health() -> dict:
    """Report the most recent tracking write failure, if any. Health endpoint
    exposes this so ops can spot silently-broken persistence (e.g. disk full,
    permission loss) before it produces duplicate digest emails."""
    return {"ok": _last_write_error is None, "last_error": _last_write_error, "last_error_at": _last_write_error_at}


def get_latest_events(transaction_ids: list[str]) -> dict[str, dict]:
    if not transaction_ids:
        return {}
    empty = {evt + "_at": None for evt in EVENT_TYPES}
    result = {tx_id: dict(empty) for tx_id in transaction_ids}
    placeholders = ",".join("?" * len(transaction_ids))
    try:
        with contextlib.closing(_connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT transaction_id, event_type, MAX(occurred_at)
                FROM invoice_events
                WHERE transaction_id IN ({placeholders})
                GROUP BY transaction_id, event_type
                """,
                transaction_ids,
            ).fetchall()
        for tx_id, event_type, occurred_at in rows:
            key = f"{event_type}_at"
            if key in result[tx_id]:
                result[tx_id][key] = occurred_at
    except Exception as exc:
        print(f"WARNING: tracking read failed: {exc}")
    return result


def latest_event_time(event_type: str) -> str | None:
    """Return the most recent occurred_at (ISO string) for a given event_type,
    or None if the DB has no events of that type. Used to give the UI a sensible
    'last sent' fallback after in-memory state is lost on restart."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event_type {event_type!r}; expected one of {EVENT_TYPES}")
    try:
        with contextlib.closing(_connect()) as conn:
            row = conn.execute(
                "SELECT MAX(occurred_at) FROM invoice_events WHERE event_type = ?",
                (event_type,),
            ).fetchone()
        return row[0] if row else None
    except Exception as exc:
        print(f"WARNING: tracking read failed: {exc}")
        return None


def get_unemailed_ids(candidate_ids: list[str]) -> list[str]:
    """Return the subset of `candidate_ids` that have no 'emailed' event yet.
    Order is preserved from the input list."""
    if not candidate_ids:
        return []
    placeholders = ",".join("?" * len(candidate_ids))
    try:
        with contextlib.closing(_connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT transaction_id FROM invoice_events
                WHERE event_type = 'emailed' AND transaction_id IN ({placeholders})
                """,
                candidate_ids,
            ).fetchall()
        emailed = {r[0] for r in rows}
    except Exception as exc:
        print(f"WARNING: tracking read failed: {exc}")
        return []
    return [tid for tid in candidate_ids if tid not in emailed]


def get_unpushed_ids(candidate_ids: list[str]) -> list[str]:
    """Return the subset of `candidate_ids` with no 'netsuite' event yet — i.e.
    not yet pushed to NetSuite. Order preserved. Used by the auto-push job so a
    scheduled run never re-pushes what a manual push already sent."""
    if not candidate_ids:
        return []
    placeholders = ",".join("?" * len(candidate_ids))
    try:
        with contextlib.closing(_connect()) as conn:
            rows = conn.execute(
                f"""SELECT DISTINCT transaction_id FROM invoice_events
                    WHERE event_type = 'netsuite' AND transaction_id IN ({placeholders})""",
                candidate_ids,
            ).fetchall()
        pushed = {r[0] for r in rows}
    except Exception as exc:
        print(f"WARNING: tracking read failed: {exc}")
        return []
    return [tid for tid in candidate_ids if tid not in pushed]


def record_job_run(job: str, status: str, detail: str = "") -> None:
    """Append a scheduled-job run to the durable log (job_runs). Best-effort."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with contextlib.closing(_connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO job_runs (job, status, detail, ran_at) VALUES (?, ?, ?, ?)",
                    (job, status, detail, now),
                )
    except Exception as exc:
        print(f"ERROR: job_runs write failed for {job!r}: {exc}")


def recent_job_runs(limit: int = 50, job: str | None = None) -> list[dict]:
    """Most-recent scheduled-job runs, newest first. Optionally filter by job."""
    try:
        with contextlib.closing(_connect()) as conn:
            if job:
                rows = conn.execute(
                    "SELECT job, status, detail, ran_at FROM job_runs WHERE job = ? "
                    "ORDER BY id DESC LIMIT ?", (job, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT job, status, detail, ran_at FROM job_runs "
                    "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"job": r[0], "status": r[1], "detail": r[2], "ran_at": r[3]} for r in rows]
    except Exception as exc:
        print(f"WARNING: job_runs read failed: {exc}")
        return []


def record_netsuite_push(external_id: str, netsuite_id: str | None, last_modified: str | None) -> None:
    """Remember what we last wrote to NetSuite under this externalId: the record's
    internal id and its lastModifiedDate. The lastModifiedDate is the guard the
    NEXT push compares against (optimistic lock -- see NetSuiteClient.upsert_invoice
    and OMIS's NetsuiteTransaction). Best-effort; a failure never fails a send."""
    if not external_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        with contextlib.closing(_connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO netsuite_records (external_id, netsuite_id, last_modified, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(external_id) DO UPDATE SET "
                    "netsuite_id = excluded.netsuite_id, last_modified = excluded.last_modified, "
                    "updated_at = excluded.updated_at",
                    (external_id, netsuite_id, last_modified, now))
    except Exception as exc:
        print(f"ERROR: netsuite_records write failed for {external_id!r}: {exc}")


def get_netsuite_last_modified(external_id: str) -> str | None:
    """The lastModifiedDate recorded the last time we wrote this externalId, or
    None if we have never pushed it (no guard baseline -> the update proceeds)."""
    try:
        with contextlib.closing(_connect()) as conn:
            row = conn.execute(
                "SELECT last_modified FROM netsuite_records WHERE external_id = ?",
                (external_id,)).fetchone()
        return row[0] if row else None
    except Exception as exc:
        print(f"WARNING: netsuite_records read failed for {external_id!r}: {exc}")
        return None


def get_netsuite_ids(external_ids: list[str]) -> dict[str, str]:
    """{external_id: netsuite_id} for the given externalIds we have pushed -- used
    to build direct links to each SO in the accounting digest. Missing/failed reads
    just omit the id (the digest falls back to a search-by-Lead# note)."""
    ids = [e for e in external_ids if e]
    if not ids:
        return {}
    try:
        with contextlib.closing(_connect()) as conn:
            placeholders = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT external_id, netsuite_id FROM netsuite_records WHERE external_id IN ({placeholders})",
                ids).fetchall()
        return {r[0]: r[1] for r in rows if r[1]}
    except Exception as exc:
        print(f"WARNING: netsuite_records batch read failed: {exc}")
        return {}


def record_finale_invoice(transaction_id: str, po_number: str | None, invoice_id: str | None,
                          invoice_url: str | None, invoice_id_user: str | None, status: str,
                          created_by: str | None = None, finale_total: float | None = None,
                          delta: float | None = None) -> None:
    """Remember the Finale invoice for this Crstl transaction: its id/url and status --
    'posted' / 'draft' when we created it, 'external' when someone else had already
    invoiced the order (by hand, or another integration) -- plus who created it, its
    total and the delta vs the 810 total, so the digest can reconcile all three
    systems. Keyed on transaction_id so a re-run sees it and never creates a second
    invoice (Finale's collection POST always creates -- proven 2026-09-15). Best-effort."""
    if not transaction_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        with contextlib.closing(_connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO finale_invoices (transaction_id, po_number, invoice_id, invoice_url, "
                    "invoice_id_user, status, updated_at, created_by, finale_total, delta) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(transaction_id) DO UPDATE SET po_number = excluded.po_number, "
                    "invoice_id = excluded.invoice_id, invoice_url = excluded.invoice_url, "
                    "invoice_id_user = excluded.invoice_id_user, status = excluded.status, "
                    "updated_at = excluded.updated_at, created_by = excluded.created_by, "
                    "finale_total = excluded.finale_total, delta = excluded.delta",
                    (transaction_id, po_number, invoice_id, invoice_url, invoice_id_user, status, now,
                     created_by, finale_total, delta))
    except Exception as exc:
        print(f"ERROR: finale_invoices write failed for {transaction_id!r}: {exc}")


def record_finale_shipment(asn_id: str, po_number: str | None, shipment_id: str | None,
                           pro: str | None, rts: str | None, status: str) -> None:
    """Remember what the DSD pass did for this 856: the Finale shipment it wrote the
    PRO/RTS onto ('prefilled'), or why it stopped for good ('skipped_equal' -- already
    there; 'skipped_shipped' -- shipped before we got to it). Keyed on the ASN id so a
    re-run never rewrites the same shipment. Best-effort."""
    if not asn_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        with contextlib.closing(_connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO finale_shipments (asn_id, po_number, shipment_id, pro, rts, status, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(asn_id) DO UPDATE SET po_number = excluded.po_number, "
                    "shipment_id = excluded.shipment_id, pro = excluded.pro, rts = excluded.rts, "
                    "status = excluded.status, updated_at = excluded.updated_at",
                    (asn_id, po_number, shipment_id, pro, rts, status, now))
    except Exception as exc:
        print(f"ERROR: finale_shipments write failed for {asn_id!r}: {exc}")


def record_alert(key: str, po_number: str | None, issue: str) -> None:
    """One receipt per alerted outlier (key = issue:shipment id), so it is emailed once."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with contextlib.closing(_connect()) as conn:
            with conn:
                conn.execute("INSERT INTO alerts (key, po_number, issue, sent_at) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET sent_at = excluded.sent_at, resolved_at = NULL", (key, po_number, issue, now))
    except Exception as exc:
        print(f"ERROR: alerts write failed for {key!r}: {exc}")


def resolve_alert(key: str) -> None:
    try:
        with contextlib.closing(_connect()) as conn:
            with conn:
                conn.execute("UPDATE alerts SET resolved_at = ? WHERE key = ? AND resolved_at IS NULL",
                             (datetime.now(timezone.utc).isoformat(), key))
    except Exception as exc:
        print(f"ERROR: alerts resolve failed for {key!r}: {exc}")


def get_alert_receipts(keys: list[str]) -> dict[str, dict]:
    ks = [k for k in keys if k]
    if not ks:
        return {}
    try:
        with contextlib.closing(_connect()) as conn:
            rows = conn.execute(f"SELECT key, po_number, issue, sent_at, resolved_at FROM alerts WHERE key IN ({','.join('?' * len(ks))})", ks).fetchall()
        return {r[0]: {"key": r[0], "po_number": r[1], "issue": r[2], "sent_at": r[3], "resolved_at": r[4]} for r in rows}
    except Exception as exc:
        print(f"WARNING: alerts read failed: {exc}")
        return {}


def open_alert_receipts() -> list[dict]:
    """Every alerted outlier not yet resolved, oldest first."""
    try:
        with contextlib.closing(_connect()) as conn:
            rows = conn.execute("SELECT key, po_number, issue, sent_at, resolved_at FROM alerts WHERE resolved_at IS NULL ORDER BY sent_at").fetchall()
        return [{"key": r[0], "po_number": r[1], "issue": r[2], "sent_at": r[3], "resolved_at": r[4]} for r in rows]
    except Exception as exc:
        print(f"WARNING: alerts read failed: {exc}")
        return []


def record_push_snapshot(transaction_id: str, invoice_number: str | None, target: str, hd_total) -> None:
    """What the 810 was worth when we sent it to `target` ("netsuite" / "finale").

    CRSTL can edit an ACCEPTED invoice in place, reusing the transaction id (proven
    2026-09-22: six SK invoices lost their PST that way, after we had pushed them). The
    push then skips them as already done and NetSuite/Finale keep the old figures
    silently. This snapshot is what makes that divergence visible. Best-effort."""
    if not transaction_id or hd_total is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        with contextlib.closing(_connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO push_snapshots (key, transaction_id, invoice_number, target, hd_total, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET hd_total = excluded.hd_total, "
                    "recorded_at = excluded.recorded_at",
                    (f"{transaction_id}:{target}", transaction_id, invoice_number, target, float(hd_total), now))
    except Exception as exc:
        print(f"ERROR: push_snapshots write failed for {transaction_id}/{target}: {exc}")


def get_push_snapshots(transaction_ids: list[str]) -> dict[str, dict]:
    """{transaction_id: {target: {"hd_total", "recorded_at"}}} for these transactions."""
    ids = [str(t) for t in transaction_ids if t]
    if not ids:
        return {}
    out: dict[str, dict] = {}
    try:
        with contextlib.closing(_connect()) as conn:
            for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
                rows = conn.execute(
                    f"SELECT transaction_id, target, hd_total, recorded_at FROM push_snapshots "
                    f"WHERE transaction_id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
                for tx, target, total, at in rows:
                    out.setdefault(tx, {})[target] = {"hd_total": total, "recorded_at": at}
    except Exception as exc:
        print(f"WARNING: push_snapshots read failed: {exc}")
    return out


def get_invoice_checks() -> dict[str, dict]:
    """Every invoice-check finding ever recorded (app.invoice_checks), keyed by
    'invoice_number:issue'. Open rows have resolved_at None."""
    try:
        with contextlib.closing(_connect()) as conn:
            rows = conn.execute("SELECT key, invoice_number, issue, problem, first_seen, resolved_at FROM invoice_checks").fetchall()
        return {r[0]: {"key": r[0], "invoice_number": r[1], "issue": r[2], "problem": r[3],
                       "first_seen": r[4], "resolved_at": r[5]} for r in rows}
    except Exception as exc:
        print(f"WARNING: invoice_checks read failed: {exc}")
        return {}


def sync_invoice_checks(current: list[dict]) -> None:
    """Record today's findings AFTER the digest that showed them went out: a new (or
    reopened) finding gets first_seen = now, an open one keeps its date, and an open
    finding no longer present is resolved. `current` = [{key, invoice_number, issue,
    problem}]. The caller must not pass an empty list because the invoice data failed
    to load -- that would clear every open finding."""
    now = datetime.now(timezone.utc).isoformat()
    keys = [c["key"] for c in current]
    try:
        with contextlib.closing(_connect()) as conn:
            with conn:
                for c in current:
                    conn.execute(
                        "INSERT INTO invoice_checks (key, invoice_number, issue, problem, first_seen) VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET problem = excluded.problem, "
                        "first_seen = CASE WHEN invoice_checks.resolved_at IS NULL THEN invoice_checks.first_seen ELSE excluded.first_seen END, "
                        "resolved_at = NULL",
                        (c["key"], c["invoice_number"], c["issue"], c.get("problem"), now))
                open_keys = [r[0] for r in conn.execute("SELECT key FROM invoice_checks WHERE resolved_at IS NULL")]
                for k in open_keys:
                    if k not in keys:
                        conn.execute("UPDATE invoice_checks SET resolved_at = ? WHERE key = ?", (now, k))
    except Exception as exc:
        print(f"ERROR: invoice_checks write failed: {exc}")


def record_shipstation_mark(order_id: str, order_number: str | None, tracking: str | None,
                            ship_date: str | None, status: str) -> None:
    """Remember that this ShipStation order was marked shipped by the DSD close pass
    (status 'closed'), keyed on ShipStation's orderId so it is done once. Best-effort."""
    if not order_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        with contextlib.closing(_connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO shipstation_marks (order_id, order_number, tracking, ship_date, status, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(order_id) DO UPDATE SET order_number = excluded.order_number, "
                    "tracking = excluded.tracking, ship_date = excluded.ship_date, status = excluded.status, "
                    "updated_at = excluded.updated_at",
                    (order_id, order_number, tracking, ship_date, status, now))
    except Exception as exc:
        print(f"ERROR: shipstation_marks write failed for {order_id!r}: {exc}")


def record_dropship_marks(marks: list[dict]) -> None:
    """Remember that these packed dropship shipments carry this tracking + carrier --
    written by the pre-fill, or read back and found already there -- so the next poll
    need not read them again. Best-effort: a lost mark only costs a re-read."""
    if not marks:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        with contextlib.closing(_connect()) as conn:
            with conn:
                conn.executemany(
                    "INSERT INTO dropship_marks (shipment_url, po_number, tracking, carrier_url, marked_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(shipment_url) DO UPDATE SET po_number = excluded.po_number, "
                    "tracking = excluded.tracking, carrier_url = excluded.carrier_url, marked_at = excluded.marked_at",
                    [(m["shipment_url"], m.get("po_number"), m["tracking"], m["carrier_url"], now) for m in marks])
    except Exception as exc:
        print(f"ERROR: dropship_marks write failed ({len(marks)} rows): {exc}")


def get_dropship_marks() -> dict[str, tuple[str, str]]:
    """{shipment_url: (tracking, carrier_url)} for every dropship shipment the pre-fill
    has filled in or found filled in."""
    try:
        with contextlib.closing(_connect()) as conn:
            rows = conn.execute("SELECT shipment_url, tracking, carrier_url FROM dropship_marks").fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}
    except Exception as exc:
        print(f"WARNING: dropship_marks read failed: {exc}")
        return {}


def get_shipstation_marks(order_ids: list[str]) -> dict[str, dict]:
    """{order_id: receipt row} for the ShipStation orders the close pass has done."""
    ids = [i for i in order_ids if i]
    if not ids:
        return {}
    try:
        with contextlib.closing(_connect()) as conn:
            rows = conn.execute(
                f"SELECT order_id, order_number, tracking, ship_date, status, updated_at FROM shipstation_marks "
                f"WHERE order_id IN ({','.join('?' * len(ids))})", ids).fetchall()
        return {r[0]: {"order_number": r[1], "tracking": r[2], "ship_date": r[3], "status": r[4], "updated_at": r[5]} for r in rows}
    except Exception as exc:
        print(f"WARNING: shipstation_marks read failed: {exc}")
        return {}


def get_finale_shipments(asn_ids: list[str]) -> dict[str, dict]:
    """{asn_id: receipt row} for the ASNs the DSD pass has already dealt with."""
    if not asn_ids:
        return {}
    placeholders = ",".join("?" * len(asn_ids))
    try:
        with contextlib.closing(_connect()) as conn:
            rows = conn.execute(
                f"SELECT asn_id, po_number, shipment_id, pro, rts, status, updated_at "
                f"FROM finale_shipments WHERE asn_id IN ({placeholders})", asn_ids).fetchall()
    except Exception as exc:
        print(f"WARNING: tracking read failed: {exc}")
        return {}
    keys = ("asn_id", "po_number", "shipment_id", "pro", "rts", "status", "updated_at")
    return {r[0]: dict(zip(keys, r)) for r in rows}


def get_unprefilled_asn_ids(candidate_ids: list[str]) -> list[str]:
    """The subset of `candidate_ids` with no finale_shipments receipt yet. Order
    preserved. Mirrors get_unfinaled_ids."""
    if not candidate_ids:
        return []
    done = set(get_finale_shipments(candidate_ids))
    return [aid for aid in candidate_ids if aid not in done]


_FINALE_COLS = ("transaction_id, po_number, invoice_id, invoice_url, invoice_id_user, status, updated_at, "
                "created_by, finale_total, delta")


def _finale_row(r) -> dict:
    return {"po_number": r[1], "invoice_id": r[2], "invoice_url": r[3], "invoice_id_user": r[4],
            "status": r[5], "updated_at": r[6], "created_by": r[7], "finale_total": r[8], "delta": r[9]}


def get_finale_invoices(transaction_ids: list[str]) -> dict[str, dict]:
    """{transaction_id: {invoice_id, invoice_url, invoice_id_user, status, updated_at}}
    for the transactions we have created a Finale invoice for. Drives idempotency
    (skip what already exists) and the digest's 'Finale invoice' column."""
    ids = [t for t in transaction_ids if t]
    if not ids:
        return {}
    try:
        with contextlib.closing(_connect()) as conn:
            placeholders = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT {_FINALE_COLS} FROM finale_invoices WHERE transaction_id IN ({placeholders})", ids).fetchall()
        return {r[0]: _finale_row(r) for r in rows}
    except Exception as exc:
        print(f"WARNING: finale_invoices batch read failed: {exc}")
        return {}


def get_unfinaled_ids(candidate_ids: list[str]) -> list[str]:
    """The subset of `candidate_ids` with no 'finale' event yet -- not yet invoiced
    in Finale by this tool. Order preserved. Mirrors get_unpushed_ids."""
    if not candidate_ids:
        return []
    placeholders = ",".join("?" * len(candidate_ids))
    try:
        with contextlib.closing(_connect()) as conn:
            rows = conn.execute(
                f"""SELECT DISTINCT transaction_id FROM invoice_events
                    WHERE event_type = 'finale' AND transaction_id IN ({placeholders})""",
                candidate_ids).fetchall()
        done = {r[0] for r in rows}
    except Exception as exc:
        print(f"WARNING: tracking read failed: {exc}")
        return []
    return [tid for tid in candidate_ids if tid not in done]


def recent_finale_invoices(since_iso: str) -> list[dict]:
    """Finale invoice receipts written since `since_iso` (UTC ISO), newest first.
    Keys starting "order:" are non-EDI orders (no Crstl transaction); the rest are
    Crstl transaction ids. Feeds the digest's Finale lines."""
    try:
        with contextlib.closing(_connect()) as conn:
            rows = conn.execute(
                f"SELECT {_FINALE_COLS} FROM finale_invoices WHERE updated_at >= ? ORDER BY updated_at DESC",
                (since_iso,)).fetchall()
        return [{"key": r[0], **_finale_row(r)} for r in rows]
    except Exception as exc:
        print(f"WARNING: finale_invoices recent read failed: {exc}")
        return []
