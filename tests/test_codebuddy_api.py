"""Tests for the CodeBuddy router API client. Run: python -m tests.test_codebuddy_api"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_register.codebuddy.api import RouterClient, RouterError


def _mock_response(status_code=200, json_body=None, cookies=None):
    resp = MagicMock()
    resp.ok = status_code < 400
    resp.status_code = status_code
    resp.text = "mock body"
    resp.json.return_value = json_body or {}
    resp.cookies = MagicMock()
    resp.cookies.get = MagicMock(return_value=cookies)
    return resp


def test_login_success():
    client = RouterClient("https://router.test/api", "secret", log=lambda m: None)
    mock_resp = _mock_response(200, {"ok": True}, cookies="eyJtoken123")
    with patch.object(client.session, "post", return_value=mock_resp):
        token = client.login()
    assert token == "eyJtoken123"
    assert client.get_auth_token() == "eyJtoken123"


def test_login_no_token_raises():
    client = RouterClient("https://router.test/api", "secret")
    mock_resp = _mock_response(200, {"ok": True}, cookies=None)
    with patch.object(client.session, "post", return_value=mock_resp):
        try:
            client.login()
        except RouterError as exc:
            assert "no auth_token" in str(exc).lower()
        else:
            raise AssertionError("must raise when no token returned")


def test_login_http_error_raises():
    client = RouterClient("https://router.test/api", "secret")
    mock_resp = _mock_response(500, {"error": "server error"}, cookies=None)
    with patch.object(client.session, "post", return_value=mock_resp):
        try:
            client.login()
        except RouterError as exc:
            assert "500" in str(exc)
        else:
            raise AssertionError("must raise on HTTP 500")


def test_request_device_code_success():
    client = RouterClient("https://router.test/api", "secret")
    client._auth_token = "token123"
    mock_resp = _mock_response(200, {
        "device_code": "dev-123",
        "verification_uri": "https://www.codebuddy.ai/auth?device_code=dev-123",
        "user_code": "",
        "interval": 5,
        "_isCodeBuddy": True,
        "codeVerifier": None,
    })
    with patch.object(client.session, "get", return_value=mock_resp):
        data = client.request_device_code()
    assert data["device_code"] == "dev-123"
    assert "codebuddy.ai" in data["verification_uri"]


def test_request_device_code_missing_fields():
    client = RouterClient("https://router.test/api", "secret")
    client._auth_token = "token123"
    mock_resp = _mock_response(200, {"foo": "bar"})
    with patch.object(client.session, "get", return_value=mock_resp):
        try:
            client.request_device_code()
        except RouterError as exc:
            assert "missing" in str(exc).lower()
        else:
            raise AssertionError("must raise when response missing device_code")


def test_request_device_code_without_login():
    client = RouterClient("https://router.test/api", "secret")
    try:
        client.request_device_code()
    except RouterError as exc:
        assert "login" in str(exc).lower()
    else:
        raise AssertionError("must raise when login() not called")


def test_poll_success():
    client = RouterClient("https://router.test/api", "secret")
    client._auth_token = "token123"
    mock_resp = _mock_response(200, {
        "success": True,
        "connection": {"id": 42, "provider": "codebuddy-intl"},
    })
    with patch.object(client.session, "post", return_value=mock_resp):
        result = client.poll("dev-123", interval=0, timeout=5)
    assert result["success"] is True
    assert result["connection"]["id"] == 42


def test_poll_pending_then_success():
    client = RouterClient("https://router.test/api", "secret")
    client._auth_token = "token123"
    # first call: pending, second call: success
    pending_resp = _mock_response(200, {"success": False, "pending": True, "error": "authorization_pending"})
    success_resp = _mock_response(200, {
        "success": True,
        "connection": {"id": 99, "provider": "codebuddy-intl"},
    })
    with patch.object(client.session, "post", side_effect=[pending_resp, success_resp]):
        result = client.poll("dev-123", interval=0, timeout=10)
    assert result["success"] is True
    assert result["connection"]["id"] == 99


def test_poll_timeout():
    client = RouterClient("https://router.test/api", "secret")
    client._auth_token = "token123"
    pending_resp = _mock_response(200, {"success": False, "pending": True, "error": "authorization_pending"})
    with patch.object(client.session, "post", return_value=pending_resp):
        result = client.poll("dev-123", interval=0, timeout=2)
    assert result["success"] is False
    assert result["error"] == "timeout"
    assert result["pending"] is False


def test_poll_hard_error_stops():
    client = RouterClient("https://router.test/api", "secret")
    client._auth_token = "token123"
    error_resp = _mock_response(200, {"success": False, "pending": False, "error": "access_denied"})
    with patch.object(client.session, "post", return_value=error_resp):
        result = client.poll("dev-123", interval=0, timeout=10)
    assert result["success"] is False
    assert result["error"] == "access_denied"
    assert result["pending"] is False


def test_poll_cancel():
    client = RouterClient("https://router.test/api", "secret")
    client._auth_token = "token123"
    pending_resp = _mock_response(200, {"success": False, "pending": True, "error": "authorization_pending"})
    with patch.object(client.session, "post", return_value=pending_resp):
        result = client.poll("dev-123", interval=0, timeout=10, cancel_cb=lambda: True)
    assert result["success"] is False
    assert result["error"] == "cancelled"


def test_login_url_no_double_api():
    """Ensure login URL is base_url + /auth/login (no double /api)."""
    client = RouterClient("https://router.test/api", "secret")
    captured_url = {}

    mock_resp = _mock_response(200, {"ok": True}, cookies="tok")
    def capture_post(url, *a, **kw):
        captured_url["url"] = url
        return mock_resp

    with patch.object(client.session, "post", side_effect=capture_post):
        client.login()
    assert captured_url["url"] == "https://router.test/api/auth/login"
    assert "/api/api/" not in captured_url["url"]


if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        fn()
        print(f"[OK] {name}")
    print("[*] all codebuddy api tests passed")
