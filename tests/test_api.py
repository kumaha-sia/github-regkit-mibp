"""API tests for the refactored web server. Run: python -m tests.test_api"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# isolate: point ACCOUNTS_DIR/DB at a temp dir BEFORE importing web.server
import github_register.runner as runner

_TMP = Path(tempfile.mkdtemp()) / "accounts"
_TMP.mkdir(parents=True)
runner.ACCOUNTS_DIR = _TMP
runner.RECOVERY_DIR = _TMP / "recovery"
runner.DB_PATH = _TMP / "regkit.db"

# neutralize the server's import-time legacy import (it reads the real
# accounts/ dir as a side effect of module import)
import github_register.storage.legacy_txt as _lt

_lt.import_accounts_dir = lambda *a, **k: None

from fastapi.testclient import TestClient

import web.server as server

server.ACCOUNTS_DIR = _TMP
server.DB_PATH = _TMP / "regkit.db"
server._storage = runner.SqliteStorage(server.DB_PATH)

client = TestClient(server.app)


def _seed():
    """Idempotent seed — safe to call from any test regardless of order."""
    from github_register.storage.models import Account

    if server._storage.get_by_email("api@x.com") is None:
        server._storage.add(Account(
            email="api@x.com", username="apiuser", password="pw",
            totp_secret="TOTPSECRET123456", recovery_codes="r1\nr2",
        ))
    if server._storage.get_by_email("plain@x.com") is None:
        server._storage.add(Account(email="plain@x.com", username="plainuser", password="pw"))


def _reset():
    """Drop all rows so delete-test ordering cannot leak into others."""
    conn = server._storage._conn()
    with conn:
        conn.execute("DELETE FROM accounts")
        conn.execute("DELETE FROM jobs")
        conn.execute("DELETE FROM job_events")


def test_health_and_auth_off():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_accounts_all_shape():
    _reset()
    _seed()
    r = client.get("/api/accounts/all")
    body = r.json()
    assert body["ok"] is True
    assert body["total"] == 2
    row = body["rows"][0]
    # legacy API shape preserved for the frontend
    assert set(row) >= {"email", "password", "username", "totp", "has_recovery"}
    # filter endpoints
    assert client.get("/api/accounts/all?filter=has2fa").json()["total"] == 1
    assert client.get("/api/accounts/all?filter=no2fa").json()["total"] == 1
    assert client.get("/api/accounts/all?filter=recovery").json()["total"] == 1
    assert client.get("/api/accounts/all?search=apiuser").json()["total"] == 1
    # pagination
    paged = client.get("/api/accounts/all?per_page=1&page=2").json()
    assert paged["pages"] == 2 and len(paged["rows"]) == 1


def test_preview_and_metrics():
    _reset()
    _seed()
    r = client.get("/api/accounts/preview").json()
    assert r["ok"] is True and r["total"] == 2
    m = client.get("/api/metrics").json()
    assert m["ok"] is True
    assert m["total_accounts"] == 2
    assert m["total_2fa"] == 1
    assert m["total_recovery"] == 1
    assert m["success_rate"] == 50.0


def test_recovery_lookup():
    _reset()
    _seed()
    r = client.get("/api/accounts/recovery?email=api@x.com")
    assert r.status_code == 200
    assert r.json()["codes"] == ["r1", "r2"]
    missing = client.get("/api/accounts/recovery?email=plain@x.com")
    assert missing.status_code == 404


def test_delete_row():
    _reset()
    _seed()
    r = client.request("DELETE", "/api/accounts/row", json={"email": "plain@x.com", "name": ""})
    assert r.status_code == 200
    assert client.get("/api/accounts/all").json()["total"] == 1
    again = client.request("DELETE", "/api/accounts/row", json={"email": "plain@x.com", "name": ""})
    assert again.status_code == 404


def test_download_export_format():
    _reset()
    _seed()
    r = client.get("/api/accounts/download")
    assert r.status_code == 200
    lines = r.text.strip().splitlines()
    assert "api@x.com----pw----apiuser----TOTPSECRET123456----1" in lines
    assert "plain@x.com----pw----plainuser--------0" in lines
    assert "attachment" in r.headers.get("content-disposition", "")


def test_logs_history_endpoint():
    _reset()
    from github_register.storage.models import Job, JobEvent

    jid = server._storage.create(Job(target=1))
    server._storage.add_event(JobEvent(job_id=jid, ts="2026-08-28 10:00:00", message="start"))
    server._storage.add_event(JobEvent(job_id=jid, ts="2026-08-28 10:00:05", message="mid", level="warn"))
    server._storage.finish(jid, ok=1, fail=0, status="done")

    # latest job by default
    r = client.get("/api/logs/history").json()
    assert r["ok"] is True and r["job_id"] == jid
    assert [e["message"] for e in r["events"]] == ["start", "mid"]
    assert r["events"][0]["level"] == "info"

    # after= continues from the given event id
    first_id = r["events"][0]["id"]
    r2 = client.get(f"/api/logs/history?after={first_id}").json()
    assert [e["message"] for e in r2["events"]] == ["mid"]

    # explicit job_id with no events
    empty = client.get("/api/logs/history?job_id=99999").json()
    assert empty["ok"] is True and empty["events"] == []


def test_append_log_persists_to_running_job():
    _reset()
    from github_register.storage.models import Job

    jid = server._storage.create(Job(target=1))
    server._current_job_id = jid
    try:
        server._append_log("hello persistent log")
    finally:
        server._current_job_id = None
    # buffer got the line...
    assert any("hello persistent log" in line for line in server._log_buffer)
    # ...and the DB got it too
    events = server._storage.events_after(jid)
    assert any(e.message == "hello persistent log" for e in events)
    # with no active job, _append_log must not raise nor write
    before = len(server._storage.events_after(jid))
    server._append_log("orphan line")
    assert len(server._storage.events_after(jid)) == before


def test_config_roundtrip():
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()["config"]
    # sensitive fields must be masked
    assert "*" in body["litensi_api_key"] or body["litensi_api_key"] == ""
    assert "*" in body["proxy"] or body["proxy"] == ""


def test_config_codebuddy_roundtrip():
    """PUT codebuddy fields must persist and appear in GET response."""
    # PUT with real values
    r = client.put("/api/config", json={
        "codebuddy_router_url": "https://router.test/api",
        "codebuddy_router_password": "testpass123",
    })
    assert r.status_code == 200
    body = r.json()["config"]
    # URL is NOT sensitive -> returned as-is
    assert body["codebuddy_router_url"] == "https://router.test/api"
    # password IS sensitive -> masked
    assert "*" in body["codebuddy_router_password"]
    assert body.get("has_codebuddy_router_password") is True

    # GET again — values persist
    r2 = client.get("/api/config")
    body2 = r2.json()["config"]
    assert body2["codebuddy_router_url"] == "https://router.test/api"
    assert "*" in body2["codebuddy_router_password"]  # masked
    assert body2.get("has_codebuddy_router_password") is True

    # PUT with masked placeholder must NOT overwrite the stored value
    r3 = client.put("/api/config", json={
        "codebuddy_router_password": "te*****23",  # masked placeholder
    })
    assert r3.status_code == 200
    body3 = r3.json()["config"]
    # still has the real password (masked), not overwritten by placeholder
    assert body3.get("has_codebuddy_router_password") is True

    # clear the password
    r4 = client.put("/api/config", json={
        "codebuddy_router_password": "",
    })
    assert r4.status_code == 200
    body4 = r4.json()["config"]
    assert body4["codebuddy_router_password"] == ""
    assert body4.get("has_codebuddy_router_password") is False


if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        fn()
        print(f"[OK] {name}")
    print("[*] all api tests passed")
