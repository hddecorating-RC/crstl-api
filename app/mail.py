"""Microsoft Graph mail sender.

Uses the client-credentials OAuth flow (server-to-server, no user interaction).
Requires an Azure AD app registration with `Mail.Send` Application permission
and admin consent granted. See README/setup docs for the Azure setup steps.

Env vars:
    GRAPH_TENANT_ID     — Azure AD directory (tenant) id
    GRAPH_CLIENT_ID     — App registration application (client) id
    GRAPH_CLIENT_SECRET — Client secret value
    MAIL_SENDER         — Mailbox to send from (e.g. dashboards@hddecorating.com)
"""
import base64
import os

import msal
import requests


GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class MailConfigError(RuntimeError):
    """Raised when required env vars are missing."""


def _config() -> dict:
    missing = [k for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "MAIL_SENDER") if not os.environ.get(k)]
    if missing:
        raise MailConfigError(f"missing env vars: {', '.join(missing)}")
    return {
        "tenant_id":     os.environ["GRAPH_TENANT_ID"],
        "client_id":     os.environ["GRAPH_CLIENT_ID"],
        "client_secret": os.environ["GRAPH_CLIENT_SECRET"],
        "sender":        os.environ["MAIL_SENDER"],
    }


def _acquire_token(cfg: dict) -> str:
    """Get a Graph API access token via client credentials. MSAL handles caching."""
    app = msal.ConfidentialClientApplication(
        cfg["client_id"],
        authority=f"https://login.microsoftonline.com/{cfg['tenant_id']}",
        client_credential=cfg["client_secret"],
    )
    result = app.acquire_token_for_client(scopes=[GRAPH_SCOPE])
    if "access_token" not in result:
        raise RuntimeError(f"Graph token acquisition failed: {result.get('error_description') or result}")
    return result["access_token"]


def send_mail(
    subject: str,
    body_html: str,
    recipients: list[str],
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> None:
    """Send an email via Microsoft Graph.

    attachments: list of (filename, content_bytes, mime_type) tuples.
    Raises MailConfigError if env vars are missing, or RuntimeError on send failure.
    """
    cfg = _config()
    token = _acquire_token(cfg)

    message = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body_html},
        "toRecipients": [{"emailAddress": {"address": addr}} for addr in recipients],
    }

    if attachments:
        message["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": name,
                "contentType": mime,
                "contentBytes": base64.b64encode(content).decode("ascii"),
            }
            for name, content, mime in attachments
        ]

    resp = requests.post(
        f"{GRAPH_BASE}/users/{cfg['sender']}/sendMail",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"message": message, "saveToSentItems": True},
        timeout=30,
    )
    if resp.status_code != 202:
        raise RuntimeError(f"Graph sendMail failed ({resp.status_code}): {resp.text[:300]}")
