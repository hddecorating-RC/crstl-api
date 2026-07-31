import base64
import pytest
from unittest.mock import patch, MagicMock

from app.mail import send_mail, MailConfigError


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GRAPH_TENANT_ID", "tenant-uuid")
    monkeypatch.setenv("GRAPH_CLIENT_ID", "client-uuid")
    monkeypatch.setenv("GRAPH_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("MAIL_SENDER", "dashboards@hddecorating.com")


def test_missing_env_raises_config_error(monkeypatch):
    for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "MAIL_SENDER"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(MailConfigError):
        send_mail("subj", "<p>x</p>", ["a@b.com"])


@patch("app.mail.msal.ConfidentialClientApplication")
@patch("app.mail.requests.post")
def test_sends_message_with_attachment(mock_post, mock_msal, env):
    mock_msal.return_value.acquire_token_for_client.return_value = {"access_token": "tok123"}
    mock_post.return_value.status_code = 202

    send_mail(
        subject="Test",
        body_html="<p>hello</p>",
        recipients=["accounting@hddecorating.com", "cc@hddecorating.com"],
        attachments=[("invoices.csv", b"col1,col2\n1,2\n", "text/csv")],
    )

    args, kwargs = mock_post.call_args
    assert args[0] == "https://graph.microsoft.com/v1.0/users/dashboards@hddecorating.com/sendMail"
    assert kwargs["headers"]["Authorization"] == "Bearer tok123"
    body = kwargs["json"]
    assert body["saveToSentItems"] is True
    msg = body["message"]
    assert msg["subject"] == "Test"
    assert [r["emailAddress"]["address"] for r in msg["toRecipients"]] == ["accounting@hddecorating.com", "cc@hddecorating.com"]
    assert len(msg["attachments"]) == 1
    att = msg["attachments"][0]
    assert att["name"] == "invoices.csv"
    assert att["contentType"] == "text/csv"
    assert base64.b64decode(att["contentBytes"]) == b"col1,col2\n1,2\n"


@patch("app.mail.msal.ConfidentialClientApplication")
@patch("app.mail.requests.post")
def test_no_attachments_omits_attachments_field(mock_post, mock_msal, env):
    mock_msal.return_value.acquire_token_for_client.return_value = {"access_token": "tok"}
    mock_post.return_value.status_code = 202
    send_mail("s", "<p>x</p>", ["a@b.com"])
    msg = mock_post.call_args.kwargs["json"]["message"]
    assert "attachments" not in msg


@patch("app.mail.msal.ConfidentialClientApplication")
@patch("app.mail.requests.post")
def test_non_202_raises(mock_post, mock_msal, env):
    mock_msal.return_value.acquire_token_for_client.return_value = {"access_token": "tok"}
    mock_post.return_value.status_code = 401
    mock_post.return_value.text = '{"error":"unauthorized"}'
    with pytest.raises(RuntimeError, match="401"):
        send_mail("s", "<p>x</p>", ["a@b.com"])


@patch("app.mail.msal.ConfidentialClientApplication")
def test_token_failure_raises(mock_msal, env):
    mock_msal.return_value.acquire_token_for_client.return_value = {
        "error": "invalid_client", "error_description": "AADSTS7000215: bad secret"
    }
    with pytest.raises(RuntimeError, match="Graph token"):
        send_mail("s", "<p>x</p>", ["a@b.com"])
