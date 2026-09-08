"""
Tests for the NetSuite TBA connector. None of these need real credentials or a
network: the OAuth signing is pure, and the transport is exercised with a fake
session. This is the whole point of scaffolding the connector now -- the signing
math and the request shape are locked down before a single credential exists.
"""
import pytest

from app.netsuite_client import (
    NetSuiteClient,
    NetSuiteUnavailable,
    signature_base_string,
)
from app.netsuite_payload import build_invoice_payload


CREDS = dict(
    account_id="1234567",
    consumer_key="ck",
    consumer_secret="cs",
    token_id="tk",
    token_secret="ts",
)


def test_signature_base_string_is_exact():
    """The base string is where a wrong encoding or ordering silently breaks
    signing, so pin it to a hand-computed value."""
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
    """A GET with a query string must sign that query too, sorted in with the
    oauth params -- test_connection relies on this for ?limit=1."""
    oauth = {"oauth_nonce": "n", "oauth_timestamp": "1"}
    base = signature_base_string(
        "GET", "https://x.suitetalk.api.netsuite.com/services/rest/record/v1/invoice?limit=1", oauth
    )
    # limit sorts before the oauth_ keys; the path is signed without the query.
    assert "limit%3D1%26oauth_nonce%3Dn%26oauth_timestamp%3D1" in base
    assert "%3Flimit" not in base  # the "?" is not part of the signed base URL


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
    assert sandbox.realm == "1234567_SB1"  # realm keeps the underscore form


def test_auth_header_is_deterministic_and_well_formed():
    client = NetSuiteClient(**CREDS)
    url = f"{client.base_url}/record/v1/invoice"
    h1 = client._auth_header("GET", url, nonce="fixed", timestamp=1700000000)
    h2 = client._auth_header("GET", url, nonce="fixed", timestamp=1700000000)
    assert h1 == h2  # same inputs -> same signature
    assert h1.startswith('OAuth realm="1234567"')
    for field in ("oauth_consumer_key", "oauth_signature_method", "oauth_signature", "oauth_token"):
        assert field in h1
    # a different token secret must change the signature
    other = NetSuiteClient(**{**CREDS, "token_secret": "different"})
    assert other._auth_header("GET", url, nonce="fixed", timestamp=1700000000) != h1


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
    fake = _FakeSession(_FakeResponse(status_code=204, headers={"Location": "/record/v1/invoice/987"}))
    client.session = fake

    result = client.upsert_invoice({"externalId": "TXN-123", "entity": {"id": "4147"}})

    call = fake.calls[0]
    assert call["method"] == "PUT"
    assert call["url"].endswith("/services/rest/record/v1/invoice/eid:TXN-123")
    assert call["json"] == {"externalId": "TXN-123", "entity": {"id": "4147"}}
    assert "Authorization" in call["headers"] and call["headers"]["Authorization"].startswith("OAuth ")
    assert result["location"] == "/record/v1/invoice/987"  # 204 -> status+location


def test_http_error_raises_netsuite_unavailable():
    client = NetSuiteClient(**CREDS)
    client.session = _FakeSession(_FakeResponse(status_code=400, content=b'{"detail":"bad"}'))
    with pytest.raises(NetSuiteUnavailable):
        client.upsert_invoice({"externalId": "X"})


def test_build_invoice_payload_from_transform_lines():
    """The payload builder consumes exactly what transform_invoice emits."""
    lines = [
        {"external_id": "TXN-1", "customer_id": "4147", "tran_date": "2026-09-01",
         "due_date": "2026-10-01", "memo": "INV1 / PO 55", "other_ref_num": "55",
         "currency": "CAD", "tax_code": "CA-HST ONT", "item": "Merchandise Sales",
         "description": "", "quantity": 1, "rate": 100.0, "amount": 100.0, "tax_amount": 13.0},
        {"external_id": "TXN-1", "customer_id": "4147", "tran_date": "2026-09-01",
         "due_date": "2026-10-01", "memo": "INV1 / PO 55", "other_ref_num": "55",
         "currency": "CAD", "tax_code": "CA-HST ONT", "item": "Allowance",
         "description": "OI10", "quantity": 1, "rate": -5.0, "amount": -5.0, "tax_amount": 0},
    ]
    body = build_invoice_payload(lines)
    assert body["externalId"] == "TXN-1"
    assert body["entity"] == {"id": "4147"}
    assert len(body["item"]["items"]) == 2
    assert body["item"]["items"][1]["amount"] == -5.0


def test_build_invoice_payload_rejects_empty():
    with pytest.raises(ValueError):
        build_invoice_payload([])
