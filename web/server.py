#!/usr/bin/env python3
"""FastAPI control plane for the GitHub register toolkit."""
from __future__ import annotations

import asyncio
import collections
import hmac
import json
import os
import secrets
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_DIR = ROOT / "accounts"
RECOVERY_DIR = ACCOUNTS_DIR / "recovery"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from github_register.config import Config, load_config, save_config, SENSITIVE_FIELDS
from github_register.crypto import encrypt, decrypt, is_enabled as crypto_enabled
from github_register.litensi import LitensiClient, LitensiError
from github_register.notifier import send_notification, format_job_message
from github_register.validator import validate_account, validate_totp
from github_register.runner import run_job, silence_playwright_noise
from github_register.storage.legacy_txt import export_accounts_txt, import_accounts_dir
from github_register.storage.models import JobEvent
from github_register.storage.sqlite import SqliteStorage

silence_playwright_noise()  # hide TargetClosedError spam when browsers close

ACCESS_PASSWORD = (os.getenv("GITHUB_REGISTER_ACCESS_PASSWORD") or "").strip()
HOST = (os.getenv("GITHUB_REGISTER_HOST") or "127.0.0.1").strip()
PORT = int(os.getenv("GITHUB_REGISTER_PORT") or "8093")  # 8092 is used by grok-regkit (Chromium)

DIST = ROOT / "frontend" / "dist"
DB_PATH = ACCOUNTS_DIR / "regkit.db"


def _migrate_legacy_account_files() -> None:
    """Move pre-accounts/ output files once, preserving existing account data."""
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    for legacy in ROOT.glob("github_accounts_*.txt"):
        target = ACCOUNTS_DIR / legacy.name
        if not target.exists():
            legacy.replace(target)


_migrate_legacy_account_files()

# single storage handle for the whole process (per-thread connections inside)
_storage = SqliteStorage(DB_PATH)

# one-shot legacy import: txt files -> SQLite. Idempotent (duplicates skipped),
# so it is safe to run on every boot; it only adds rows it has never seen.
import_accounts_dir(ACCOUNTS_DIR, _storage, log=lambda m: print(m, file=sys.stderr))

_sessions: Dict[str, float] = {}
_SESSION_TTL = 86400 * 7

# Rate limiter for login attempts: max 5 attempts per 60s per IP
_login_attempts: Dict[str, collections.deque] = {}
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SEC = 60


def _check_login_rate_limit(client_ip: str) -> bool:
    """Return True if request is allowed, False if rate-limited."""
    now = time.time()
    attempts = _login_attempts.get(client_ip)
    if attempts is None:
        attempts = collections.deque()
        _login_attempts[client_ip] = attempts
    # purge old entries
    while attempts and attempts[0] < now - _LOGIN_WINDOW_SEC:
        attempts.popleft()
    if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
        return False
    attempts.append(now)
    return True

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
    "accounts": [],  # per-account status: [{email, status: pending|running|done|failed, reason}]
}

app = FastAPI(title="GitHub Register", version="1.0.0")


class StopController:
    def __init__(self) -> None:
        self._stop = False

    def should_stop(self) -> bool:
        return self._stop

    def stop(self) -> None:
        self._stop = True


_current_job_id: Optional[int] = None  # job whose events _append_log persists


def _append_log(message: str) -> None:
    global _log_seq
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    with _log_cond:
        _log_buffer.append(line)
        _log_seq += 1
        _log_cond.notify_all()
    # persist into the running job's event stream; failures must never
    # break the job thread's logging path
    if _current_job_id is not None:
        try:
            _storage.add_event(JobEvent(
                job_id=_current_job_id,
                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                message=message,
                level="info",
            ))
        except Exception:
            pass


def _mask_value(key: str, value: Any) -> Any:
    if key not in SENSITIVE_FIELDS:
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
    for key in SENSITIVE_FIELDS:
        raw = getattr(cfg, key, "")
        masked[f"has_{key}"] = bool(str(raw or "").strip())
    return masked


def _require_auth(x_access_key: Optional[str]) -> None:
    if not ACCESS_PASSWORD:
        return
    key = (x_access_key or "").strip()
    if not key:
        raise HTTPException(status_code=401, detail="access key required")
    # constant-time comparison to prevent timing attacks
    if hmac.compare_digest(key, ACCESS_PASSWORD):
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
    email_provider: Optional[str] = None
    litensi_api_id: Optional[str] = None
    litensi_api_key: Optional[str] = None
    litensi_site: Optional[str] = None
    litensi_zone: Optional[str] = None
    tempik_api_base: Optional[str] = None
    tempik_domains: Optional[str] = None
    register_count: Optional[int] = None
    proxy_mode: Optional[str] = None
    proxy: Optional[str] = None
    proxy_list: Optional[str] = None
    proxy_file: Optional[str] = None
    headless: Optional[bool] = None
    delay_sec: Optional[float] = None
    max_username_tries: Optional[int] = None
    otp_timeout_sec: Optional[int] = None
    browser_profile_dir: Optional[str] = None
    fresh_profile: Optional[bool] = None
    proxy_hard_block_retries: Optional[int] = None
    proxy_rate_limit_retries: Optional[int] = None
    rotate_ip_per_account: Optional[bool] = None
    create_repo: Optional[bool] = None
    repo_name: Optional[str] = None
    enable_2fa: Optional[bool] = None
    set_profile_status: Optional[bool] = None
    profile_status: Optional[str] = None
    complete_profile: Optional[bool] = None
    profile_name: Optional[str] = None
    profile_bio: Optional[str] = None
    profile_location: Optional[str] = None
    notify_url: Optional[str] = None
    notify_token: Optional[str] = None
    router_url: Optional[str] = None
    router_password: Optional[str] = None
    schedule_cron: Optional[str] = None
    schedule_count: Optional[int] = None


def _save_config(cfg: Config) -> None:
    save_config(cfg, ROOT / "config.json")


def _run_job(count: int) -> None:
    global _controller, _current_job_id
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

    def _on_progress(ok_count: int, fail_count: int) -> None:
        # called by run_job() after each account attempt (and on start/finish)
        with _job_lock:
            _job_state["success"] = int(ok_count)
            _job_state["fail"] = int(fail_count)

    def _on_job_id(job_id: int) -> None:
        # bind _append_log's persistence to this job's event stream
        global _current_job_id
        _current_job_id = job_id

    try:
        cfg = load_config(ROOT / "config.json")
        cfg.register_count = count
        ok, fail, out = run_job(
            cfg,
            cancel_cb=controller.should_stop,
            log=_append_log,
            progress_cb=_on_progress,
            job_id_cb=_on_job_id,
        )
        with _job_lock:
            _job_state.update(success=ok, fail=fail, accounts_file=str(out))
        # send webhook notification if configured
        if getattr(cfg, "notify_url", "") and getattr(cfg, "notify_token", "") or getattr(cfg, "notify_url", ""):
            msg = format_job_message(ok, fail, count, str(out) if out else "")
            send_notification(
                getattr(cfg, "notify_url", ""),
                getattr(cfg, "notify_token", ""),
                msg, ok, fail, count, str(out) if out else "",
            )
    except Exception as exc:
        _append_log(f"[!] job error: {exc}")
        with _job_lock:
            _job_state["error"] = str(exc)
    finally:
        global _current_job_id
        _current_job_id = None
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
async def api_auth(body: AuthBody, request: Request) -> Dict[str, Any]:
    if not ACCESS_PASSWORD:
        return {"ok": True, "needs_auth": False, "token": ""}
    client_ip = request.client.host if request.client else "unknown"
    if not _check_login_rate_limit(client_ip):
        return JSONResponse(
            {"ok": False, "detail": "Too many login attempts. Please wait a minute."},
            status_code=429,
        )
    # constant-time comparison
    if not hmac.compare_digest((body.password or "").strip(), ACCESS_PASSWORD):
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
        if key in SENSITIVE_FIELDS and isinstance(value, str):
            stripped = value.strip()
            if stripped == "":
                setattr(cfg, key, "")
                continue
            if "*" in stripped:  # masked placeholder from GET â€” keep previous
                continue
        setattr(cfg, key, value)
    _save_config(cfg)
    return {"ok": True, "config": _public_config()}


class ProxiesBody(BaseModel):
    content: str = ""


def _proxy_file_path() -> Path:
    """Resolve the proxy file path from config (relative to ROOT)."""
    cfg = load_config(ROOT / "config.json")
    p = Path(getattr(cfg, "proxy_file", "proxies.txt") or "proxies.txt")
    return p if p.is_absolute() else ROOT / p


@app.get("/api/proxies")
async def api_get_proxies(x_access_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    """Read the proxy list file content."""
    _require_auth(x_access_key)
    path = _proxy_file_path()
    content = ""
    if path.is_file():
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"cannot read proxy file: {exc}")
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    return {"ok": True, "content": content, "count": len(lines), "path": path.name}


@app.put("/api/proxies")
async def api_put_proxies(
    body: ProxiesBody, x_access_key: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """Write the proxy list to the file."""
    _require_auth(x_access_key)
    path = _proxy_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.content or "", encoding="utf-8")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"cannot write proxy file: {exc}")
    lines = [l.strip() for l in (body.content or "").splitlines() if l.strip()]
    return {"ok": True, "count": len(lines), "path": path.name}


class LitensiZonesBody(BaseModel):
    """Optional overrides so the user can test credentials/site BEFORE saving.

    Any field left None (or a masked '*' placeholder for the API key) falls back
    to the value already stored in config.json.
    """
    litensi_api_id: Optional[str] = None
    litensi_api_key: Optional[str] = None
    litensi_site: Optional[str] = None


@app.post("/api/litensi/zones")
async def api_litensi_zones(
    body: LitensiZonesBody, x_access_key: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """Return the list of Litensi mail zones for the given site.

    Uses overrides from the request body when provided; otherwise falls back to
    the credentials/site stored in config.json. Masked values (containing '*')
    coming back from the UI are ignored (treated as "unchanged").
    """
    _require_auth(x_access_key)
    cfg = load_config(ROOT / "config.json")

    def _resolve(override: Optional[str], fallback: str, *, secret: bool = False) -> str:
        if override is None:
            return fallback or ""
        s = override.strip()
        if not s:
            return fallback or ""
        if secret and "*" in s:
            return fallback or ""
        return s

    api_id = _resolve(body.litensi_api_id, cfg.litensi_api_id)
    api_key = _resolve(body.litensi_api_key, cfg.litensi_api_key, secret=True)
    site = _resolve(body.litensi_site, cfg.litensi_site)

    if not api_id or not api_key:
        raise HTTPException(status_code=400, detail="Litensi API ID / API Key is not configured")
    if not site:
        raise HTTPException(status_code=400, detail="Site domain is not configured")

    try:
        client = LitensiClient(api_id, api_key, site, zone="")
        zones = client.prices()
    except LitensiError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # network / unexpected
        raise HTTPException(status_code=502, detail=f"Unable to contact Litensi: {exc}")

    # normalize: keep only known-useful fields, coerce numerics safely
    def _num(v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    normalized: List[Dict[str, Any]] = []
    for z in zones:
        if not isinstance(z, dict):
            continue
        normalized.append({
            "zone": str(z.get("zone") or ""),
            "price": _num(z.get("price")),
            "stock": _num(z.get("stock")),
            # keep original raw fields too for forward-compat display
            "raw": z,
        })

    # pick cheapest in-stock zone (same rule as pick_zone) for UI highlight
    in_stock = [z for z in normalized if z["stock"] > 0]
    cheapest = min(in_stock, key=lambda z: z["price"])["zone"] if in_stock else ""

    return {
        "ok": True,
        "site": site,
        "zones": normalized,
        "cheapest": cheapest,
        "current_zone": cfg.litensi_zone or "",
    }


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


@app.get("/api/logs/history")
async def api_logs_history(
    x_access_key: Optional[str] = Header(None),
    job_id: Optional[int] = Query(None, ge=1),
    after: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=5000),
) -> Dict[str, Any]:
    """Persistent log events from the database (survives restarts).

    Without job_id: the most recent job is used. `after` enables streaming
    continuation by event id.
    """
    _require_auth(x_access_key)
    try:
        if job_id is None:
            latest = _storage.latest()
            if latest is None:
                return {"ok": True, "job_id": None, "events": [], "total": 0}
            job_id = latest.id
        events = _storage.events_after(job_id, after_id=after, limit=limit)
        return {
            "ok": True,
            "job_id": job_id,
            "events": [
                {"id": e.id, "ts": e.ts, "level": e.level, "message": e.message}
                for e in events
            ],
            "total": len(events),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"cannot read job events: {exc}")


def _account_row(a) -> Dict[str, Any]:
    """API shape kept identical to the legacy file parser output.

    `file`/`file_mtime` derive from the account's created_at so the
    frontend's batch column keeps rendering without changes.
    """
    stamp = (a.created_at or "").replace("-", "").replace(":", "").replace(" ", "_")
    batch = f"github_accounts_{stamp.split('_')[0]}_{stamp.split('_')[1][:6]}.txt" if "_" in stamp else "regkit.db"
    from datetime import datetime as _dt

    try:
        mtime = _dt.strptime(a.created_at, "%Y-%m-%d %H:%M:%S").timestamp() if a.created_at else 0
    except ValueError:
        mtime = 0
    return {
        "email": a.email,
        "password": a.password,
        "username": a.username,
        "totp": a.totp_secret,
        "has_recovery": bool(a.recovery_codes),
        "status": a.status,
        "created_at": a.created_at,
        "file": batch,
        "file_mtime": mtime,
    }


@app.get("/api/accounts")
async def api_accounts_list(x_access_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    """Account batches (legacy files + DB jobs share this listing)."""
    _require_auth(x_access_key)
    files = sorted(ACCOUNTS_DIR.glob("github_accounts_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    items = [
        {"name": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime}
        for f in files[:50]
    ]
    return {"ok": True, "files": items, "total_accounts": _storage.count()}


def _read_accounts_file(path: Path) -> str:
    """Read an accounts file, decrypting if encryption is enabled."""
    raw = path.read_text(encoding="utf-8")
    if raw.startswith("enc:"):
        return decrypt(raw)
    return raw



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
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
) -> Dict[str, Any]:
    """Account rows (newest first) for the export panel."""
    _require_auth(x_access_key)
    result = _storage.list(page=page, per_page=per_page)
    rows = [_account_row(a) for a in result.rows]
    return {
        "ok": True,
        "rows": rows,
        "total": result.total,
        "name": (Path(name).name if name else "") or "regkit.db",
        "page": result.page,
        "pages": result.pages,
    }


@app.get("/api/accounts/all")
async def api_accounts_all(
    x_access_key: Optional[str] = Header(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
    search: str = Query("", max_length=200),
    filter: str = Query("all", pattern="^(all|has2fa|no2fa|recovery)$"),
) -> Dict[str, Any]:
    """All accounts with pagination, search, and filter — served from SQLite."""
    _require_auth(x_access_key)
    result = _storage.list(page=page, per_page=per_page, search=search, filter=filter)
    return {
        "ok": True,
        "rows": [_account_row(a) for a in result.rows],
        "total": result.total,
        "page": result.page,
        "per_page": result.per_page,
        "pages": result.pages,
    }


@app.get("/api/accounts/recovery")
async def api_accounts_recovery(
    email: str = Query(..., min_length=3, max_length=320),
    x_access_key: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Read recovery codes for exactly one account, if they were captured."""
    _require_auth(x_access_key)
    account = _storage.get_by_email(email)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    codes = [line.strip() for line in account.recovery_codes.splitlines() if line.strip()]
    if not codes:
        raise HTTPException(status_code=404, detail="recovery codes are not available for this account")
    return {"ok": True, "email": account.email, "codes": codes}


class DeleteRowBody(BaseModel):
    email: str
    name: str = ""  # kept for frontend compat; deletion is DB-wide by email


@app.delete("/api/accounts/row")
async def api_accounts_delete_row(
    body: DeleteRowBody, x_access_key: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """Delete one account (by email) from the database."""
    _require_auth(x_access_key)
    deleted = _storage.delete(body.email)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"row not found: {body.email}")
    return {"ok": True, "deleted": 1, "remaining": _storage.count()}


@app.get("/api/metrics")
async def api_metrics(
    x_access_key: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Dashboard metrics: total accounts, success rate, breakdown."""
    _require_auth(x_access_key)
    total_accounts = _storage.count()
    total_2fa = _storage.count("has2fa")
    total_recovery = _storage.count("recovery")
    daily = _storage.daily_counts(days=30)
    return {
        "ok": True,
        "total_accounts": total_accounts,
        "total_2fa": total_2fa,
        "total_recovery": total_recovery,
        "total_files": 1,  # single source of truth now
        "success_rate": round(total_2fa / total_accounts * 100, 1) if total_accounts else 0,
        "daily": dict(sorted(daily.items())[-30:]),  # last 30 days
    }


@app.delete("/api/accounts/file")
async def api_accounts_delete_file(
    x_access_key: Optional[str] = Header(None),
    name: str = Query(...),
) -> Dict[str, Any]:
    """Delete an entire legacy accounts file (DB rows stay authoritative)."""
    _require_auth(x_access_key)
    safe = Path(name).name
    path = ACCOUNTS_DIR / safe
    if not safe.startswith("github_accounts_") or not safe.endswith(".txt") or not path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    path.unlink()
    return {"ok": True, "deleted": safe}


class ValidateBody(BaseModel):
    email: str
    password: str
    totp: str = ""


@app.post("/api/accounts/validate")
async def api_validate_account(
    body: ValidateBody,
    x_access_key: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Validate a registered account by attempting login + TOTP check."""
    _require_auth(x_access_key)
    result = validate_account(body.email, body.password, body.totp)
    return {"ok": True, **result}


@app.get("/api/accounts/download")
async def api_accounts_download(
    x_access_key: Optional[str] = Header(None),
    filter: str = Query("all", pattern="^(all|has2fa|no2fa|recovery)$"),
) -> Response:
    """Export all accounts (optionally filtered) in the legacy txt format.

    Generated on the fly from SQLite — the '----' file format is now an
    export artifact, not the storage backend.
    """
    _require_auth(x_access_key)
    result = _storage.list(page=1, per_page=10_000, filter=filter)
    text = export_accounts_txt(result.rows)
    filename = f"github_accounts_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="{filename}"'},
    )


if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(DIST / "assets")), name="assets")


def main() -> None:
    import uvicorn

    uvicorn.run("web.server:app", host=HOST, port=PORT, workers=1, log_level="info")


if __name__ == "__main__":
    main()
