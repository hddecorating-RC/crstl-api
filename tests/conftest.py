"""Suite-wide isolation. Why this exists (2026-09-16): the pytest-dotenv plugin loads
the repo's .env -- REAL ShipStation / Finale / CRSTL / NetSuite credentials -- into
every test process, and a poll test that patched the EDI and non-EDI passes but not
the new ShipStation pass ran it LIVE: two DSD orders were marked shipped in
ShipStation from a developer's laptop during `pytest`. Nothing here may ever reach a
real system, so every live credential is scrubbed before each test, and the
automated passes that gate on config are forced OFF unless a test enables them
explicitly with its own patch."""
import os
from unittest.mock import patch

import pytest

LIVE_ENV = ("SHIPSTATION_KEY", "SHIPSTATION_V1_KEY", "SHIPSTATION_V1_SECRET",
            "FINALE_ACCOUNT_ID", "FINALE_API_KEY", "FINALE_API_SECRET",
            "CRSTL_API_KEY", "NETSUITE_ACCOUNT_ID", "NETSUITE_CONSUMER_KEY", "NETSUITE_CONSUMER_SECRET",
            "NETSUITE_TOKEN_ID", "NETSUITE_TOKEN_SECRET", "GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET",
            "MAIL_SENDER", "MAIL_RECIPIENTS", "ALERT_RECIPIENTS")


@pytest.fixture(autouse=True)
def _no_live_credentials(monkeypatch):
    for k in LIVE_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("PYTEST_RUNNING", "1")
    yield


@pytest.fixture(autouse=True)
def _config_gated_passes_off():
    """ShipStation close is OFF for every test; a test that wants it on patches
    app.main._shipstation_config itself (an inner patch wins)."""
    with patch("app.finale_jobs._shipstation_config", return_value={"enabled": False}), \
         patch("app.alert_jobs._alerts_config", return_value={"enabled": False}), \
         patch("app.finale_jobs._dropship_config", return_value={"enabled": False}):
        yield
