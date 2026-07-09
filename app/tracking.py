import contextlib
import os
import pathlib
import sqlite3
from datetime import datetime, timezone


def _db_path() -> str:
    return os.environ.get("TRACKING_DB", ".tmp/tracking.db")


def init_db() -> None:
    path = _db_path()
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(_connect()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS invoice_events (
                transaction_id TEXT NOT NULL,
                event_type     TEXT NOT NULL CHECK(event_type IN ('exported', 'netsuite')),
                occurred_at    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_invoice_events_tx
                ON invoice_events(transaction_id);
        """)


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(_db_path(), check_same_thread=False)


def record_events(transaction_ids: list[str], event_type: str) -> None:
    if not transaction_ids:
        return
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
    result = {tx_id: {"exported_at": None, "netsuite_at": None} for tx_id in transaction_ids}
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
            if event_type == "exported":
                result[tx_id]["exported_at"] = occurred_at
            elif event_type == "netsuite":
                result[tx_id]["netsuite_at"] = occurred_at
    except Exception as exc:
        print(f"WARNING: tracking read failed: {exc}")
    return result
