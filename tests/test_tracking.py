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
        "tx-001": {"exported_at": None, "netsuite_at": None},
        "tx-002": {"exported_at": None, "netsuite_at": None},
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
