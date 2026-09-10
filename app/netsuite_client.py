"""
NetSuite REST connector, Token-Based Authentication (TBA / OAuth 1.0a).

This fills the placeholder that the CSV export (app/netsuite_csv.py +
tools/generate_accounting_csv.py) stood in for. The CSV path stays as the
fallback for a host with no NetSuite credentials; this is the automated path
once TBA credentials exist.

TBA is NetSuite's server-to-server auth: no password is ever sent. Every request
is signed with TWO key pairs -- the consumer pair (which application) and the
token pair (which user + role) -- plus the account id. All five come from a
NetSuite admin: an Integration record yields the consumer key/secret, an Access
Token yields the token id/secret. See .env.example for the names.

Nothing here needs those credentials to import or to unit-test. The signing is
pure and covered by tests/test_netsuite_client.py; only an actual send needs the
five values. Keeping the transport correct-but-credential-free is the point of
scaffolding it now: the mapping (app/netsuite.py) and payload
(app/netsuite_payload.py) can be exercised end to end in a dry run today.
"""
import base64
import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

import requests

ENV_KEYS = (
    "NETSUITE_ACCOUNT_ID",
    "NETSUITE_CONSUMER_KEY",
    "NETSUITE_CONSUMER_SECRET",
    "NETSUITE_TOKEN_ID",
    "NETSUITE_TOKEN_SECRET",
)


class NetSuiteUnavailable(RuntimeError):
    """NetSuite is not configured, or a request failed. Callers fall back to the
    CSV export path rather than crashing the run -- a missing send is a gap to
    retry, a wrong one is an error (same stance as FinaleUnavailable)."""


def _pct(value) -> str:
    """RFC-3986 percent-encoding, as OAuth 1.0 requires: everything encoded
    except the unreserved set (A-Za-z0-9 and -._~). Python's quote already leaves
    the unreserved set alone; safe="" makes it also encode "/" and ":"."""
    return quote(str(value), safe="")


def signature_base_string(method: str, url: str, oauth_params: dict) -> str:
    """The OAuth 1.0a signature base string. Pure and deterministic -- this is
    the part that silently breaks signing if the encoding or parameter ordering
    is wrong, so it is a module-level function with its own test.

    Any query string on `url` is folded into the signed parameters (OAuth
    requires it); for the record PUT/POST calls there is none.
    """
    parts = urlsplit(url)
    base_url = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))
    params = list(oauth_params.items()) + parse_qsl(parts.query, keep_blank_values=True)
    normalized = "&".join(f"{_pct(k)}={_pct(v)}" for k, v in sorted(params))
    return "&".join([method.upper(), _pct(base_url), _pct(normalized)])


class NetSuiteModifiedOnServer(RuntimeError):
    """Raised when a record we were about to update has changed in NetSuite since
    we last wrote it (optimistic lock, mirroring OMIS's NetsuiteTransaction
    last_modified_date guard). The write is aborted and the invoice flagged for
    manual review, rather than overwriting whatever changed."""


class NetSuiteNoBaseline(RuntimeError):
    """Raised when a record already EXISTS in NetSuite under our externalId but we
    have NO local baseline (lastModifiedDate) for it -- e.g. tracking.db was lost
    or restored from an old backup. We refuse to overwrite something we have no
    memory of writing (it could carry manual edits); the invoice is flagged for a
    deliberate reseed instead."""


class NetSuiteClient:
    """Minimal TBA-signed REST client for NetSuite record upserts.

    Construct with the five values explicitly (tests do), or leave them None to
    read from the environment (production does). Importing or building without
    credentials raises NetSuiteUnavailable -- deliberately, so a deployment
    without keys degrades to the CSV path instead of half-working.
    """

    TIMEOUT = 60
    SIGNATURE_METHOD = "HMAC-SHA256"

    def __init__(
        self,
        account_id=None,
        consumer_key=None,
        consumer_secret=None,
        token_id=None,
        token_secret=None,
        timeout=None,
    ):
        env = os.environ.get
        self.account_id = account_id if account_id is not None else env("NETSUITE_ACCOUNT_ID", "")
        self.consumer_key = consumer_key if consumer_key is not None else env("NETSUITE_CONSUMER_KEY", "")
        self.consumer_secret = consumer_secret if consumer_secret is not None else env("NETSUITE_CONSUMER_SECRET", "")
        self.token_id = token_id if token_id is not None else env("NETSUITE_TOKEN_ID", "")
        self.token_secret = token_secret if token_secret is not None else env("NETSUITE_TOKEN_SECRET", "")
        if not all([self.account_id, self.consumer_key, self.consumer_secret, self.token_id, self.token_secret]):
            raise NetSuiteUnavailable("NetSuite needs " + ", ".join(ENV_KEYS))

        self.timeout = timeout or self.TIMEOUT
        # In the host, the account id is lowercased and its underscore becomes a
        # hyphen (sandbox 1234567_SB1 -> 1234567-sb1). The realm keeps the
        # uppercase underscore form.
        host_account = self.account_id.lower().replace("_", "-")
        self.base_url = f"https://{host_account}.suitetalk.api.netsuite.com/services/rest"
        self.realm = self.account_id.upper()
        self.session = requests.Session()

    @staticmethod
    def configured() -> bool:
        """Whether a send can even be attempted. Check before building a client
        so callers can choose the CSV path when keys are absent."""
        return all(os.environ.get(k) for k in ENV_KEYS)

    def _auth_header(self, method: str, url: str, *, nonce=None, timestamp=None) -> str:
        oauth = {
            "oauth_consumer_key": self.consumer_key,
            "oauth_token": self.token_id,
            "oauth_signature_method": self.SIGNATURE_METHOD,
            "oauth_timestamp": str(timestamp if timestamp is not None else int(time.time())),
            "oauth_nonce": nonce or secrets.token_hex(16),
            "oauth_version": "1.0",
        }
        base = signature_base_string(method, url, oauth)
        key = f"{_pct(self.consumer_secret)}&{_pct(self.token_secret)}"
        digest = hmac.new(key.encode(), base.encode(), hashlib.sha256).digest()
        oauth["oauth_signature"] = base64.b64encode(digest).decode()
        params = ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))
        return f'OAuth realm="{self.realm}", {params}'

    def _request(self, method: str, path: str, json_body=None, extra_headers=None) -> dict:
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": self._auth_header(method, url),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        resp = self.session.request(method, url, headers=headers, json=json_body, timeout=self.timeout)
        if resp.status_code >= 400:
            raise NetSuiteUnavailable(f"NetSuite {method} {path} -> {resp.status_code}: {resp.text[:500]}")
        # An upsert returns 204 with the record's URL in Location and no body.
        if resp.content:
            try:
                return resp.json()
            except ValueError:
                pass
        return {"status": resp.status_code, "location": resp.headers.get("Location", "")}

    def upsert_record(self, record_type: str, external_id: str, body: dict) -> dict:
        """Create-or-update a record addressed by our own external id.
        PUT /record/v1/{record_type}/eid:{external_id}."""
        if not external_id:
            raise ValueError("upsert needs a non-empty external_id")
        # replace=item: overwrite the line sublist on update. Without it NetSuite
        # MERGES (appends) the lines, so re-pushing an existing invoice doubles
        # its total. With it, the upsert is truly idempotent -- lines are replaced.
        return self._request(
            "PUT", f"/record/v1/{record_type}/eid:{quote(external_id, safe='')}?replace=item", json_body=body)

    def upsert_invoice(self, record: dict, guard_last_modified: str | None = None) -> dict:
        """Upsert one NetSuite invoice. `record` is a REST invoice body carrying
        an externalId (build it with app.netsuite_payload.build_invoice_payload
        from the line records app.netsuite.transform_invoice emits). Accounting
        books this Home Depot revenue as an invoice; refs are internal ids, the
        convention OMIS uses in this same account."""
        external_id = record.get("externalId") or record.get("external_id")
        if not external_id:
            raise ValueError("invoice record needs an externalId")
        # Look before we write. The PUT (.../eid:) targets OUR externalId, so it
        # can only ever touch a record we created -- never another source's. The
        # guard adds OMIS's second layer: if the record exists and its current
        # lastModifiedDate no longer matches `guard_last_modified` (what we
        # recorded when WE last wrote it), it was changed in NetSuite since -- so
        # we ABORT rather than clobber that change.
        existing = self.get_by_external_id("invoice", external_id)
        if existing is not None:
            if guard_last_modified is None:
                # The record exists but we have no memory of writing it (tracking.db
                # lost/reset). Refuse to overwrite -- it may hold manual edits. (M2)
                raise NetSuiteNoBaseline(
                    f"invoice {external_id} exists in NetSuite but we have no local baseline "
                    "for it (tracking.db lost or reset?); refusing to overwrite -- reseed required")
            current = existing.get("lastModifiedDate")
            if current != guard_last_modified:
                raise NetSuiteModifiedOnServer(
                    f"invoice {external_id} changed in NetSuite since our last push "
                    f"(recorded {guard_last_modified!r}, now {current!r}); not overwriting")
        result = self.upsert_record("invoice", external_id, record)
        if isinstance(result, dict):
            result["action"] = "updated" if existing is not None else "created"
            # Read the record back so the caller can store the NEW lastModifiedDate
            # as the next push's guard (OMIS does the same via update_from_netsuite!).
            after = self.get_by_external_id("invoice", external_id)
            if after is not None:
                result["netsuite_id"] = after.get("id")
                result["last_modified"] = after.get("lastModifiedDate")
        return result

    def get_by_external_id(self, record_type: str, external_id: str):
        """The record under our externalId as a dict, or None on 404. Read-only --
        used to decide created-vs-updated and to read lastModifiedDate for the guard."""
        try:
            return self._request("GET", f"/record/v1/{record_type}/eid:{quote(external_id, safe='')}")
        except NetSuiteUnavailable as exc:
            if "-> 404" in str(exc):
                return None
            raise

    def get_record(self, record_type: str, record_id: str, expand: bool = True) -> dict:
        """Read one record by internal id. Read-only -- used to inspect the exact
        shape NetSuite accepts (e.g. an existing OMIS-created sales order) before
        writing anything, which stands in for the sandbox we do not have.
        expandSubResources returns the item sublist inline."""
        query = "?expandSubResources=true" if expand else ""
        return self._request("GET", f"/record/v1/{record_type}/{record_id}{query}")

    def list_records(self, record_type: str, limit: int = 5) -> dict:
        """Read-only list of a record type (ids + links). Handy for grabbing an
        existing record id to inspect."""
        return self._request("GET", f"/record/v1/{record_type}?limit={limit}")

    def suiteql(self, query: str, limit: int = 1000, offset: int = 0) -> dict:
        """Run a read-only SuiteQL SELECT (POST /query/v1/suiteql). Used to look
        up internal ids by name -- items, tax codes, classes -- so the config
        gaps can be filled from here. SELECT only; needs the role to permit REST
        queries. Returns {"items": [...], ...}."""
        return self._request(
            "POST", f"/query/v1/suiteql?limit={limit}&offset={offset}",
            json_body={"q": query}, extra_headers={"Prefer": "transient"},
        )

    def test_connection(self) -> dict:
        """Auth check that does NOT need search/list permission: fetch the invoice
        record's metadata schema. Proves the credentials, signing, and invoice
        access without reading customer data or creating anything. (A collection
        GET is a search, which some roles refuse -- observed on this account.)"""
        return self._request("GET", "/record/v1/metadata-catalog/invoice",
                             extra_headers={"Accept": "application/schema+json"})
