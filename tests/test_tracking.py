import pytest
from app.tracking import record_events, get_latest_events, init_db
from app import tracking


@pytest.fixture(autouse=True)
def _reset_write_error_state():
    """Module-level _last_write_error persists across tests; wipe it between runs."""
    tracking._last_write_error = None
    tracking._last_write_error_at = None
    yield
    tracking._last_write_error = None
    tracking._last_write_error_at = None


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


def test_latest_event_time_returns_none_when_no_events(db_path):
    from app.tracking import latest_event_time
    assert latest_event_time("emailed") is None


def test_latest_event_time_returns_most_recent(db_path):
    from app.tracking import latest_event_time
    record_events(["tx-001"], "emailed")
    first = latest_event_time("emailed")
    record_events(["tx-002"], "emailed")
    second = latest_event_time("emailed")
    assert first is not None
    assert second >= first


def test_latest_event_time_rejects_unknown_type(db_path):
    from app.tracking import latest_event_time
    with pytest.raises(ValueError):
        latest_event_time("shipped")


def test_settings_default_when_missing(db_path):
    from app.tracking import get_setting
    assert get_setting("nonexistent") is None
    assert get_setting("nonexistent", "fallback") == "fallback"


def test_settings_roundtrip_and_upsert(db_path):
    from app.tracking import get_setting, set_setting
    set_setting("auto_digest_enabled", "false")
    assert get_setting("auto_digest_enabled") == "false"
    # Upsert: second write updates the value
    set_setting("auto_digest_enabled", "true")
    assert get_setting("auto_digest_enabled") == "true"


def test_settings_survive_migration_of_events_table(tmp_path, monkeypatch):
    """The settings table must be created even when the events table already
    exists from a pre-settings deployment — otherwise the toggle endpoint
    would 500 on the first request after upgrading."""
    import sqlite3
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE invoice_events (
            transaction_id TEXT NOT NULL,
            event_type     TEXT NOT NULL,
            occurred_at    TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()

    monkeypatch.setenv("TRACKING_DB", path)
    init_db()

    from app.tracking import set_setting, get_setting
    set_setting("hello", "world")
    assert get_setting("hello") == "world"


def test_write_health_clean_when_no_failure(db_path):
    from app.tracking import write_health
    record_events(["tx-001"], "emailed")
    h = write_health()
    assert h["ok"] is True
    assert h["last_error"] is None
    assert h["last_error_at"] is None


def test_write_health_captures_failure_and_clears_on_recovery(db_path, monkeypatch):
    """If the DB is unwritable, health reports the error. A subsequent successful
    write clears it. Callers of record_events never see the exception — the
    behavior is best-effort by design so CSV downloads and email sends aren't
    aborted by a tracking hiccup."""
    from app.tracking import write_health

    # Simulate a write failure by patching _connect to raise.
    def broken_connect():
        raise sqlite3.OperationalError("disk I/O error")
    import sqlite3
    monkeypatch.setattr(tracking, "_connect", broken_connect)

    record_events(["tx-001"], "emailed")  # must not raise
    h = write_health()
    assert h["ok"] is False
    assert "disk I/O error" in h["last_error"]
    assert h["last_error"].startswith("emailed:")
    assert h["last_error_at"] is not None

    # Recovery — undo the monkeypatch and write again
    monkeypatch.undo()
    record_events(["tx-002"], "emailed")
    h = write_health()
    assert h["ok"] is True
    assert h["last_error"] is None


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
