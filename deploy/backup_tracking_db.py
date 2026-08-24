#!/usr/bin/env python3
"""Snapshot .tmp/tracking.db into .tmp/backups/ and prune old copies.

Uses SQLite's online backup API rather than `cp`. The database is live --
the digest marks invoices emailed while the API is serving -- and a plain
copy can capture a torn page mid-write. tracking.db is also the only state
that isn't reproducible from git: lose it and the next digest re-sends
every invoice to accounting.
"""
import pathlib
import sqlite3
import sys
from contextlib import closing
from datetime import date, timedelta

KEEP_DAYS = 14
TMP = pathlib.Path(__file__).resolve().parent.parent / ".tmp"
SRC = TMP / "tracking.db"
DEST_DIR = TMP / "backups"


def main() -> int:
    if not SRC.exists():
        print(f"backup: {SRC} does not exist, nothing to do")
        return 0

    DEST_DIR.mkdir(parents=True, exist_ok=True)
    dest = DEST_DIR / f"tracking.db.{date.today().isoformat()}"

    # closing() matters here -- sqlite3's own context manager commits the
    # transaction but leaves the handle open, and the integrity check below
    # must read a fully closed, flushed file.
    with closing(sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)) as src_conn:
        with closing(sqlite3.connect(dest)) as dest_conn:
            src_conn.backup(dest_conn)
            # backup() carries the source's WAL journal mode across, which
            # leaves -wal/-shm sidecars beside the snapshot. Fold them back in
            # so each snapshot is one self-contained file that can be restored
            # by copying it alone. This must run on the read-write connection:
            # a read-only one cannot checkpoint, which is how the sidecars
            # survived the first version of this script.
            dest_conn.execute("PRAGMA journal_mode=DELETE")

    # Prove the snapshot is readable before pruning anything older.
    with closing(sqlite3.connect(f"file:{dest}?mode=ro", uri=True)) as check:
        status = check.execute("PRAGMA integrity_check").fetchone()[0]
    if status != "ok":
        print(f"backup: FAILED integrity_check on {dest}: {status}", file=sys.stderr)
        return 1

    cutoff = date.today() - timedelta(days=KEEP_DAYS)
    pruned = 0
    for old in DEST_DIR.glob("tracking.db.*"):
        # Tolerate -wal/-shm suffixes so sidecars left by an older run get
        # cleaned up with the snapshot they belong to rather than accumulating.
        stamp_text = old.name[len("tracking.db."):].removesuffix("-wal").removesuffix("-shm")
        try:
            stamp = date.fromisoformat(stamp_text)
        except ValueError:
            continue  # not one of ours -- leave it alone
        if stamp < cutoff:
            old.unlink()
            pruned += 1

    print(f"backup: {dest} ({dest.stat().st_size} bytes), pruned {pruned} older than {KEEP_DAYS}d")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
