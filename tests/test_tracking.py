import pytest
from app.tracking import record_events, get_latest_events, init_db


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / ".tmp" / "tracking.db")
    monkeypatch.setenv("TRACKING_DB", path)
    init_db()
    return path


def test_get_latest_events_empty(db_path):
    result = get_latest_events(["tx-001", "tx-002"])
    assert result == {
        "tx-001": {"exported_at": None, "netsuite_at": None, "emailed_at": None},
        "tx-002": {"exported_at": None, "netsuite_at": None, "emailed_at": None},
    }


def test_record_and_get_exported(db_path):
    record_events(["tx-001", "tx-002"], "exported")
    result = get_latest_events(["tx-001", "tx-002", "tx-003"])
    assert result["tx-001"]["exported_at"] is not None
    assert result["tx-002"]["exported_at"] is not None
    assert result["tx-003"]["exported_at"] is None
    assert result["tx-001"]["netsuite_at"] is None


def test_record_updates_to_most_recent(db_path):
    record_events(["tx-001"], "exported")
    first = get_latest_events(["tx-001"])["tx-001"]["exported_at"]
    record_events(["tx-001"], "exported")
    second = get_latest_events(["tx-001"])["tx-001"]["exported_at"]
    assert second >= first


def test_record_netsuite_event(db_path):
    record_events(["tx-001"], "netsuite")
    result = get_latest_events(["tx-001"])
    assert result["tx-001"]["netsuite_at"] is not None
    assert result["tx-001"]["exported_at"] is None


def test_empty_ids_list(db_path):
    record_events([], "exported")  # should not raise
    result = get_latest_events([])
    assert result == {}


def test_record_emailed_event(db_path):
    record_events(["tx-001"], "emailed")
    result = get_latest_events(["tx-001"])
    assert result["tx-001"]["emailed_at"] is not None
    assert result["tx-001"]["exported_at"] is None


def test_unknown_event_type_raises(db_path):
    with pytest.raises(ValueError, match="unknown event_type"):
        record_events(["tx-001"], "shipped")


def test_get_unemailed_ids_filters_correctly(db_path):
    from app.tracking import get_unemailed_ids
    record_events(["tx-002", "tx-004"], "emailed")
    result = get_unemailed_ids(["tx-001", "tx-002", "tx-003", "tx-004"])
    assert result == ["tx-001", "tx-003"]


def test_get_unemailed_ids_empty(db_path):
    from app.tracking import get_unemailed_ids
    assert get_unemailed_ids([]) == []


def test_migrates_old_schema_with_check_constraint(tmp_path, monkeypatch):
    """A pre-existing DB with the old CHECK(event_type IN ('exported', 'netsuite'))
    constraint must be migrated so 'emailed' events can be written."""
    import sqlite3
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE invoice_events (
            transaction_id TEXT NOT NULL,
            event_type     TEXT NOT NULL CHECK(event_type IN ('exported', 'netsuite')),
            occurred_at    TEXT NOT NULL
        );
        INSERT INTO invoice_events VALUES ('tx-legacy', 'exported', '2026-01-01T00:00:00Z');
    """)
    conn.commit()
    conn.close()

    monkeypatch.setenv("TRACKING_DB", path)
    init_db()

    # Old row survived
    result = get_latest_events(["tx-legacy"])
    assert result["tx-legacy"]["exported_at"] == "2026-01-01T00:00:00Z"
    # New event type now writes cleanly
    record_events(["tx-new"], "emailed")
    assert get_latest_events(["tx-new"])["tx-new"]["emailed_at"] is not None
