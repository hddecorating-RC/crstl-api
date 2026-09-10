"""
Tests for the NetSuite TBA connector. None need real credentials or a network:
the OAuth signing is pure, and the transport is exercised with a fake session.
This locks down the signing math and the request shape (invoice, id refs) before
a single credential is used.
"""
import pytest

from app.netsuite_client import (
    NetSuiteClient,
    NetSuiteUnavailable,
    signature_base_string,
)
from app.netsuite_payload import build_invoice_payload, unresolved_ids


CREDS = dict(
    account_id="1234567",
    consumer_key="ck",
    consumer_secret="cs",
    token_id="tk",
    token_secret="ts",
)

# transform_invoice's output shape: two lines sharing one external_id.
LINES = [
    {"external_id": "TXN-1", "customer_id": "4147", "tran_date": "2026-09-01",
     "due_date": "2026-10-01", "memo": "INV1 / PO 55", "invoice_number": "INV1", "other_ref_num": "55",
     "currency": "CAD", "tax_code": "CA-HST ONT", "item": "Merchandise Sales",
     "description": "", "quantity": 1, "rate": 100.0, "amount": 100.0, "tax_amount": 13.0},
    {"external_id": "TXN-1", "customer_id": "4147", "tran_date": "2026-09-01",
     "due_date": "2026-10-01", "memo": "INV1 / PO 55", "invoice_number": "INV1", "other_ref_num": "55",
     "currency": "CAD", "tax_code": "CA-HST ONT", "item": "Allowance",
     "description": "OI10", "quantity": 1, "rate": -5.0, "amount": -5.0, "tax_amount": 0},
]

# internal-id maps as config would carry once filled
REFS = {
    "item_ids": {"Merchandise Sales": "201", "Allowance": "202"},
    "tax_code_ids": {"CA-HST ONT": "17"},
    "class_id": "5",
    "subsidiary_id": "",
    "custom_form_id": "101",
}


# ---- OAuth signing (the part that silently breaks if encoding/order is wrong) ----

def test_signature_base_string_is_exact():
    oauth = {
        "oauth_consumer_key": "ck",
        "oauth_token": "tk",
        "oauth_signature_method": "HMAC-SHA256",
        "oauth_timestamp": "1700000000",
        "oauth_nonce": "abc",
        "oauth_version": "1.0",
    }
    url = "https://1234567.suitetalk.api.netsuite.com/services/rest/record/v1/invoice"
    expected = (
        "GET&"
        "https%3A%2F%2F1234567.suitetalk.api.netsuite.com%2Fservices%2Frest%2Frecord%2Fv1%2Finvoice&"
        "oauth_consumer_key%3Dck%26oauth_nonce%3Dabc%26oauth_signature_method%3DHMAC-SHA256"
        "%26oauth_timestamp%3D1700000000%26oauth_token%3Dtk%26oauth_version%3D1.0"
    )
    assert signature_base_string("GET", url, oauth) == expected


def test_query_params_are_folded_into_the_signature():
    oauth = {"oauth_nonce": "n", "oauth_timestamp": "1"}
    base = signature_base_string(
        "GET", "https://x.suitetalk.api.netsuite.com/services/rest/record/v1/invoice?limit=1", oauth
    )
    assert "limit%3D1%26oauth_nonce%3Dn%26oauth_timestamp%3D1" in base
    assert "%3Flimit" not in base


def test_missing_credentials_raise():
    with pytest.raises(NetSuiteUnavailable):
        NetSuiteClient(account_id="1234567", consumer_key="", consumer_secret="cs",
                       token_id="tk", token_secret="ts")


def test_base_url_and_realm_production_vs_sandbox():
    prod = NetSuiteClient(**CREDS)
    assert prod.base_url == "https://1234567.suitetalk.api.netsuite.com/services/rest"
    assert prod.realm == "1234567"

    sandbox = NetSuiteClient(**{**CREDS, "account_id": "1234567_SB1"})
    assert sandbox.base_url == "https://1234567-sb1.suitetalk.api.netsuite.com/services/rest"
    assert sandbox.realm == "1234567_SB1"


def test_auth_header_is_deterministic_and_well_formed():
    client = NetSuiteClient(**CREDS)
    url = f"{client.base_url}/record/v1/invoice"
    h1 = client._auth_header("GET", url, nonce="fixed", timestamp=1700000000)
    h2 = client._auth_header("GET", url, nonce="fixed", timestamp=1700000000)
    assert h1 == h2
    assert h1.startswith('OAuth realm="1234567"')
    for field in ("oauth_consumer_key", "oauth_signature_method", "oauth_signature", "oauth_token"):
        assert field in h1
    other = NetSuiteClient(**{**CREDS, "token_secret": "different"})
    assert other._auth_header("GET", url, nonce="fixed", timestamp=1700000000) != h1


# ---- transport (fake session, no network) ----

class _FakeResponse:
    def __init__(self, status_code=204, headers=None, content=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content
        self.text = content.decode() if content else ""

    def json(self):
        import json
        return json.loads(self.content)


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response


def test_upsert_invoice_puts_to_the_external_id_url():
    client = NetSuiteClient(**CREDS)
    client.session = _FakeSession(_FakeResponse(200, content=b'{"id":"987","lastModifiedDate":"2026-09-10T12:00:00Z"}'))

    result = client.upsert_invoice({"externalId": "TXN-123", "entity": {"id": "4147"}}, guard_last_modified="2026-09-10T12:00:00Z")

    # GET (pre: exists + guard) -> PUT (write) -> GET (post: new baseline)
    assert [c["method"] for c in client.session.calls] == ["GET", "PUT", "GET"]
    put_call = client.session.calls[1]
    assert put_call["url"].endswith("/services/rest/record/v1/invoice/eid:TXN-123?replace=item")
    assert put_call["headers"]["Authorization"].startswith("OAuth ")
    assert result["action"] == "updated"          # the record already existed
    assert result["netsuite_id"] == "987"
    assert result["last_modified"] == "2026-09-10T12:00:00Z"


def test_get_record_is_read_only_with_expand():
    client = NetSuiteClient(**CREDS)
    client.session = _FakeSession(_FakeResponse(200, content=b'{"id":"5"}'))
    client.get_record("invoice", "5")
    call = client.session.calls[0]
    assert call["method"] == "GET"
    assert call["url"].endswith("/record/v1/invoice/5?expandSubResources=true")


def test_http_error_raises_netsuite_unavailable():
    client = NetSuiteClient(**CREDS)
    client.session = _FakeSession(_FakeResponse(400, content=b'{"detail":"bad"}'))
    with pytest.raises(NetSuiteUnavailable):
        client.upsert_invoice({"externalId": "X"})


def test_suiteql_posts_read_only_query_with_prefer_header():
    client = NetSuiteClient(**CREDS)
    client.session = _FakeSession(_FakeResponse(200, content=b'{"items":[{"id":"201"}]}'))
    result = client.suiteql("SELECT id, itemid FROM item")
    call = client.session.calls[0]
    assert call["method"] == "POST"
    assert "/query/v1/suiteql" in call["url"]
    assert call["json"] == {"q": "SELECT id, itemid FROM item"}
    assert call["headers"].get("Prefer") == "transient"
    assert result["items"][0]["id"] == "201"


# ---- payload (invoice, internal-id refs) ----

def test_build_invoice_payload_uses_internal_ids():
    body = build_invoice_payload(LINES, REFS)
    assert body["externalId"] == "TXN-1"
    assert body["otherRefNum"] == "INV1"            # LEAD # = invoice number
    assert body["custbodyinvoicepercent"] == 100    # full invoice, not a 50% deposit
    assert body["entity"] == {"id": "4147"}
    assert body["dueDate"] == "2026-10-01"          # invoices carry a due date
    assert body["class"] == {"id": "5"}
    assert body["customForm"] == {"id": "101"}      # form 101 stamped
    assert "subsidiary" not in body                 # blank -> omitted
    items = body["item"]["items"]
    assert len(items) == 2
    assert items[0]["item"] == {"id": "201"}        # resolved by name -> id
    assert items[0]["taxCode"] == {"id": "17"}
    assert items[1]["item"] == {"id": "202"}
    assert items[1]["amount"] == -5.0


def test_build_invoice_payload_omits_empty_due_date():
    lines = [dict(LINES[0], due_date="")]
    body = build_invoice_payload(lines, REFS)
    assert "dueDate" not in body  # NetSuite rejects an empty date string


def test_unresolved_ids_flags_blank_refs():
    refs = {"item_ids": {"Merchandise Sales": "201"}, "tax_code_ids": {}, "class_id": "", "subsidiary_id": ""}
    missing = unresolved_ids(LINES, refs)
    assert "item:Allowance" in missing
    assert "taxCode:CA-HST ONT" in missing
    assert "item:Merchandise Sales" not in missing


def test_build_invoice_payload_rejects_empty():
    with pytest.raises(ValueError):
        build_invoice_payload([])


def test_upsert_invoice_aborts_when_modified_on_server():
    """Optimistic lock: if the record's lastModifiedDate no longer matches the
    guard we recorded, the write is aborted (no PUT) -- we never overwrite."""
    from app.netsuite_client import NetSuiteModifiedOnServer
    client = NetSuiteClient(**CREDS)
    client.session = _FakeSession(_FakeResponse(200, content=b'{"id":"987","lastModifiedDate":"NEW"}'))
    with pytest.raises(NetSuiteModifiedOnServer):
        client.upsert_invoice({"externalId": "TXN-123"}, guard_last_modified="OLD")
    assert [c["method"] for c in client.session.calls] == ["GET"]   # only the pre-check, no PUT


def test_get_by_external_id_returns_none_on_404():
    client = NetSuiteClient(**CREDS)
    client.session = _FakeSession(_FakeResponse(404, content=b'{"detail":"not found"}'))
    assert client.get_by_external_id("invoice", "MISSING") is None


def test_upsert_invoice_percent_encodes_the_external_id():
    """H2 defense-in-depth: a special-char externalId is percent-encoded so it
    cannot inject query/path into the NetSuite URL (e.g. neutralizing replace=item)."""
    from app.netsuite_client import NetSuiteClient
    client = NetSuiteClient(**CREDS)
    client.session = _FakeSession(_FakeResponse(200, content=b'{"id":"1","lastModifiedDate":"T"}'))
    client.upsert_invoice({"externalId": "CRSTL-x?replace=none&y"}, guard_last_modified="T")
    put = [c for c in client.session.calls if c["method"] == "PUT"][0]
    assert "?replace=none" not in put["url"]          # the injected query is gone
    assert "eid:CRSTL-x%3Freplace%3Dnone%26y?replace=item" in put["url"]


def test_upsert_invoice_refuses_when_record_exists_but_no_baseline():
    """M2: a record exists in NetSuite but we have no local baseline (guard None,
    e.g. tracking.db lost) -> refuse to overwrite (no PUT)."""
    from app.netsuite_client import NetSuiteClient, NetSuiteNoBaseline
    client = NetSuiteClient(**CREDS)
    client.session = _FakeSession(_FakeResponse(200, content=b'{"id":"9","lastModifiedDate":"X"}'))
    with pytest.raises(NetSuiteNoBaseline):
        client.upsert_invoice({"externalId": "TXN-123"})   # no guard
    assert [c["method"] for c in client.session.calls] == ["GET"]   # only the pre-check, no PUT
