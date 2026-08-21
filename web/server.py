#!/usr/bin/env python3
"""FastAPI control plane for the GitHub register toolkit."""
from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import os
import secrets
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from github_register.config import Config, load_config
from github_register.runner import run_job

ACCESS_PASSWORD = (os.getenv("GITHUB_REGISTER_ACCESS_PASSWORD") or "").strip()
HOST = (os.getenv("GITHUB_REGISTER_HOST") or "127.0.0.1").strip()
PORT = int(os.getenv("GITHUB_REGISTER_PORT") or "8093")  # 8092 is used by grok-regkit (Chromium)

DIST = ROOT / "frontend" / "dist"

SECRET_FIELDS = {"litensi_api_key", "proxy"}

_sessions: Dict[str, float] = {}
_SESSION_TTL = 86400 * 7

_job_lock = threading.Lock()
_job_thread: Optional[threading.Thread] = None
_controller: Optional[Any] = None
_log_buffer: Deque[str] = collections.deque(maxlen=2000)
_log_seq = 0
_log_cond = threading.Condition()
_job_state: Dict[str, Any] = {
    "running": False,
    "success": 0,
    "fail": 0,
    "target": 0,
    "started_at": None,
    "finished_at": None,
    "error": "",
    "accounts_file": "",
}

app = FastAPI(title="GitHub Register", version="1.0.0")


class StopController:
    def __init__(self) -> None:
        self._stop = False

    def should_stop(self) -> bool:
        return self._stop

    def stop(self) -> None:
        self._stop = True


def _append_log(message: str) -> None:
    global _log_seq
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    with _log_cond:
        _log_buffer.append(line)
        _log_seq += 1
        _log_cond.notify_all()


def _mask_value(key: str, value: Any) -> Any:
    if key not in SECRET_FIELDS:
        return value
    s = "" if value is None else str(value)
    if not s:
        return ""
    if len(s) <= 6:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def _public_config() -> Dict[str, Any]:
    cfg = load_config(ROOT / "config.json")
    masked = {k: _mask_value(k, v) for k, v in asdict(cfg).items()}
    for key in SECRET_FIELDS:
        raw = getattr(cfg, key, "")
        masked[f"has_{key}"] = bool(str(raw or "").strip())
    return masked


def _require_auth(x_access_key: Optional[str]) -> None:
    if not ACCESS_PASSWORD:
        return
    key = (x_access_key or "").strip()
    if not key:
        raise HTTPException(status_code=401, detail="access key required")
    if key == ACCESS_PASSWORD:
        return
    exp = _sessions.get(key)
    if exp and exp > time.time():
        return
    if exp:
        _sessions.pop(key, None)
    raise HTTPException(status_code=403, detail="invalid access key")


def _issue_token(password: str) -> str:
    raw = f"{password}:{secrets.token_hex(16)}:{time.time()}"
    token = hashlib.sha256(raw.encode()).hexdigest()
    _sessions[token] = time.time() + _SESSION_TTL
    return token


class AuthBody(BaseModel):
    password: str = ""


class StartBody(BaseModel):
    count: int = Field(default=1, ge=1, le=1000)


class ConfigBody(BaseModel):
    litensi_api_id: Optional[str] = None
    litensi_api_key: Optional[str] = None
    litensi_site: Optional[str] = None
    litensi_zone: Optional[str] = None
    register_count: Optional[int] = None
    proxy: Optional[str] = None
    headless: Optional[bool] = None
    delay_sec: Optional[float] = None
    max_username_tries: Optional[int] = None
    otp_timeout_sec: Optional[int] = None
    browser_profile_dir: Optional[str] = None
    create_repo: Optional[bool] = None
    repo_name: Optional[str] = None
    enable_2fa: Optional[bool] = None


def _save_config(cfg: Config) -> None:
    (ROOT / "config.json").write_text(
        json.dumps(asdict(cfg), indent=4, ensure_ascii=False), encoding="utf-8"
    )


def _run_job(count: int) -> None:
    global _controller
    controller = StopController()
    with _job_lock:
        _controller = controller
        _job_state.update(
            running=True,
            success=0,
            fail=0,
            target=count,
            error="",
            started_at=time.time(),
            finished_at=None,
            accounts_file="",
        )
    try:
        cfg = load_config(ROOT / "config.json")
        cfg.register_count = count
        ok, fail, out = run_job(cfg, cancel_cb=controller.should_stop, log=_append_log)
        with _job_lock:
            _job_state.update(success=ok, fail=fail, accounts_file=str(out))
    except Exception as exc:
        _append_log(f"[!] job error: {exc}")
        with _job_lock:
            _job_state["error"] = str(exc)
    finally:
        with _job_lock:
            _job_state["running"] = False
            _job_state["finished_at"] = time.time()
            _controller = None
        _append_log("[*] web job thread finished")


@app.get("/", include_in_schema=False)
async def root() -> Response:
    index = DIST / "index.html"
    if index.is_file():
        return FileResponse(index, headers={"Cache-Control": "no-store"})
    return Response("frontend not built: run `npm run build` in frontend/", status_code=200)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "service": "github-register"}


@app.get("/monitor/status")
async def monitor_status() -> Dict[str, Any]:
    with _job_lock:
        return {"ok": True, "service": "github-register", "running_job": bool(_job_state["running"])}


@app.post("/api/auth")
async def api_auth(body: AuthBody) -> Dict[str, Any]:
    if not ACCESS_PASSWORD:
        return {"ok": True, "needs_auth": False, "token": ""}
    if (body.password or "").strip() != ACCESS_PASSWORD:
        return JSONResponse({"ok": False, "detail": "invalid password"}, status_code=403)
    return {"ok": True, "needs_auth": True, "token": _issue_token(body.password.strip())}


@app.get("/api/config")
async def api_get_config(x_access_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_auth(x_access_key)
    return {"ok": True, "config": _public_config(), "needs_auth": bool(ACCESS_PASSWORD)}


@app.put("/api/config")
async def api_put_config(body: ConfigBody, x_access_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_auth(x_access_key)
    cfg = load_config(ROOT / "config.json")
    updates = body.model_dump(exclude_unset=True)
    for key, value in updates.items():
        if key in SECRET_FIELDS and isinstance(value, str):
            stripped = value.strip()
            if stripped == "":
                setattr(cfg, key, "")
                continue
            if "*" in stripped:  # masked placeholder from GET — keep previous
                continue
        setattr(cfg, key, value)
    _save_config(cfg)
    return {"ok": True, "config": _public_config()}


@app.get("/api/status")
async def api_status(x_access_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_auth(x_access_key)
    with _job_lock:
        return {"ok": True, **_job_state}


@app.post("/api/start")
async def api_start(body: StartBody, x_access_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    global _job_thread
    _require_auth(x_access_key)
    with _job_lock:
        if _job_state["running"]:
            raise HTTPException(status_code=409, detail="job already running")
        _append_log(f"[*] starting registration count={body.count}")
        t = threading.Thread(target=_run_job, args=(body.count,), daemon=True)
        _job_thread = t
        t.start()
    return {"ok": True, "started": True, "count": body.count}


@app.post("/api/stop")
async def api_stop(x_access_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_auth(x_access_key)
    with _job_lock:
        ctrl = _controller
        running = _job_state["running"]
    if not running or ctrl is None:
        return {"ok": True, "stopped": False, "detail": "no running job"}
    ctrl.stop()
    _append_log("[!] stop requested from web")
    return {"ok": True, "stopped": True}


@app.get("/api/logs")
async def api_logs(
    request: Request,
    x_access_key: Optional[str] = Header(None),
    after: int = Query(0, ge=0),
):
    _require_auth(x_access_key)

    async def event_stream():
        last = after
        while True:
            if await request.is_disconnected():
                break
            with _log_cond:
                buf = list(_log_buffer)
                seq = _log_seq
            if seq > last:
                start_idx = max(0, len(buf) - (seq - last))
                for line in buf[start_idx:]:
                    yield f"data: {line}\n\n"
                last = seq
            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/logs/snapshot")
async def api_logs_snapshot(
    x_access_key: Optional[str] = Header(None),
    limit: int = Query(200, ge=1, le=2000),
) -> Dict[str, Any]:
    _require_auth(x_access_key)
    with _log_cond:
        lines = list(_log_buffer)[-limit:]
        seq = _log_seq
    return {"ok": True, "seq": seq, "lines": lines}


@app.get("/api/accounts")
async def api_accounts_list(x_access_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_auth(x_access_key)
    files = sorted(ROOT.glob("github_accounts_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    items = [
        {"name": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime}
        for f in files[:50]
    ]
    return {"ok": True, "files": items}


def _parse_accounts_file(path: Path) -> List[Dict[str, str]]:
    """Parse 'email----password----username[----totp]' lines into dicts."""
    rows: List[Dict[str, str]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split("----")]
            if len(parts) >= 4:
                rows.append({
                    "email": parts[0], "password": parts[1],
                    "username": parts[2], "totp": parts[3],
                })
            elif len(parts) == 3:
                rows.append({
                    "email": parts[0], "password": parts[1],
                    "username": parts[2], "totp": "",
                })
            elif len(parts) == 2:
                rows.append({
                    "email": parts[0], "password": parts[1],
                    "username": parts[0].split("@")[0], "totp": "",
                })
    except Exception:
        pass
    return rows


@app.get("/api/totp")
async def api_totp_code(
    x_access_key: Optional[str] = Header(None),
    secret: str = Query(..., min_length=16, max_length=64),
) -> Dict[str, Any]:
    """Current TOTP code + seconds remaining for a stored secret."""
    _require_auth(x_access_key)
    try:
        import pyotp

        totp = pyotp.TOTP(secret.strip())
        code = totp.now()
        remaining = totp.interval - (int(time.time()) % totp.interval)
        return {"ok": True, "code": code, "expires_in": remaining}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid TOTP secret: {exc}")


@app.get("/api/accounts/preview")
async def api_accounts_preview(
    x_access_key: Optional[str] = Header(None),
    name: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Parsed account rows of one file (or the newest) for the export panel."""
    _require_auth(x_access_key)
    if name:
        safe = Path(name).name
        path = ROOT / safe
        if not safe.startswith("github_accounts_") or not safe.endswith(".txt") or not path.is_file():
            raise HTTPException(status_code=404, detail="file not found")
    else:
        files = sorted(ROOT.glob("github_accounts_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return {"ok": True, "rows": [], "total": 0, "name": ""}
        path = files[0]
    rows = _parse_accounts_file(path)
    return {"ok": True, "rows": rows, "total": len(rows), "name": path.name}


class DeleteRowBody(BaseModel):
    email: str
    name: str  # accounts file name


@app.delete("/api/accounts/row")
async def api_accounts_delete_row(
    body: DeleteRowBody, x_access_key: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """Delete one account row (by email) from an accounts file."""
    _require_auth(x_access_key)
    safe = Path(body.name).name
    path = ROOT / safe
    if not safe.startswith("github_accounts_") or not safe.endswith(".txt") or not path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    kept = [l for l in lines if not l.strip().lower().startswith(body.email.strip().lower() + "----")]
    if len(kept) == len(lines):
        raise HTTPException(status_code=404, detail=f"row not found: {body.email}")
    path.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    return {"ok": True, "deleted": len(lines) - len(kept), "remaining": len(kept)}


@app.delete("/api/accounts/file")
async def api_accounts_delete_file(
    x_access_key: Optional[str] = Header(None),
    name: str = Query(...),
) -> Dict[str, Any]:
    """Delete an entire accounts file."""
    _require_auth(x_access_key)
    safe = Path(name).name
    path = ROOT / safe
    if not safe.startswith("github_accounts_") or not safe.endswith(".txt") or not path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    path.unlink()
    return {"ok": True, "deleted": safe}


@app.get("/api/accounts/download")
async def api_accounts_download(
    x_access_key: Optional[str] = Header(None),
    name: Optional[str] = Query(None),
) -> Response:
    _require_auth(x_access_key)
    if name:
        safe = Path(name).name
        path = ROOT / safe
        if not safe.startswith("github_accounts_") or not safe.endswith(".txt") or not path.is_file():
            raise HTTPException(status_code=404, detail="file not found")
    else:
        files = sorted(ROOT.glob("github_accounts_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            raise HTTPException(status_code=404, detail="no accounts file")
        path = files[0]
    return FileResponse(
        path,
        filename=path.name,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(DIST / "assets")), name="assets")


def main() -> None:
    import uvicorn

    uvicorn.run("web.server:app", host=HOST, port=PORT, workers=1, log_level="info")


if __name__ == "__main__":
    main()
