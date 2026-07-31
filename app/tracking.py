import contextlib
import os
import pathlib
import sqlite3
from datetime import datetime, timezone

# Event types the app writes. Enforced in application code, not via a DB CHECK
# constraint — SQLite can't ALTER a CHECK, and letting the schema outlive the
# app's event vocabulary made adding 'emailed' painful.
EVENT_TYPES = ("exported", "netsuite", "emailed")


def _db_path() -> str:
    return os.environ.get("TRACKING_DB", ".tmp/tracking.db")


def init_db() -> None:
    path = _db_path()
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(_connect()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        _create_or_migrate(conn)


def _create_or_migrate(conn: sqlite3.Connection) -> None:
    """Create the table on a fresh DB, or migrate an older schema that has a
    restrictive CHECK constraint blocking newer event types."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='invoice_events'"
    ).fetchone()

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
    return sqlite3.connect(_db_path(), check_same_thread=False)


def record_events(transaction_ids: list[str], event_type: str) -> None:
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
    except Exception as exc:
        print(f"WARNING: tracking write failed: {exc}")


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
