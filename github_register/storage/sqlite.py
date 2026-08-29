"""SQLite storage backend.

Design notes:
- One connection per thread (job thread vs uvicorn event loop) with WAL
  journaling so readers never block the writer and vice versa.
- Sensitive columns are encrypted at the application layer via crypto.py
  (Fernet from GITHUB_REGISTER_SECRET) BEFORE insertion — the database
  only ever holds 'enc:<base64>' or plaintext when encryption is off.
- Schema versioning via schema_meta so future migrations are explicit.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..crypto import decrypt, encrypt
from .models import Account, AccountPage, Job, JobEvent, SettingsEntry

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    is_secret  INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    target          INTEGER NOT NULL DEFAULT 0,
    ok              INTEGER NOT NULL DEFAULT 0,
    fail            INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'running',
    error           TEXT NOT NULL DEFAULT '',
    config_snapshot TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS accounts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    email          TEXT NOT NULL UNIQUE,
    username       TEXT NOT NULL,
    password       TEXT NOT NULL,
    totp_secret    TEXT NOT NULL DEFAULT '',
    recovery_codes TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'active',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accounts_created ON accounts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_accounts_username ON accounts(username);

CREATE TABLE IF NOT EXISTS job_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_job ON job_events(job_id, id);

CREATE TABLE IF NOT EXISTS proxy_blacklist (
    ip         TEXT PRIMARY KEY,
    blocked_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trust_cookies (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    exit_ip  TEXT NOT NULL,
    payload  TEXT NOT NULL,
    saved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS codebuddy_accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    connection_id INTEGER,
    region        TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cb_account ON codebuddy_accounts(account_id);
"""

# Columns that must be encrypted before touching the database.
_ENCRYPTED_ACCOUNT_COLUMNS = ("password", "totp_secret", "recovery_codes")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class SqliteStorage:
    """AccountRepository + JobRepository + SettingsRepository over one file."""

    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path)
        self._local = threading.local()
        self._migrate()

    # ------------------------------------------------------------------ core

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path), timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA secure_delete=ON")
            self._local.conn = conn
        return conn

    def _migrate(self) -> None:
        conn = self._conn()
        with conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('version', ?) "
                "ON CONFLICT(key) DO NOTHING",
                (SCHEMA_VERSION,),
            )

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -------------------------------------------------------------- accounts

    def _encrypt_account(self, account: Account) -> dict:
        return {
            "job_id": account.job_id,
            "email": account.email,
            "username": account.username,
            "password": encrypt(account.password),
            "totp_secret": encrypt(account.totp_secret),
            "recovery_codes": encrypt(account.recovery_codes),
            "status": account.status,
            "created_at": account.created_at or _now(),
        }

    def add(self, account: Account) -> int:
        cols = self._encrypt_account(account)
        conn = self._conn()
        with conn:
            cur = conn.execute(
                "INSERT INTO accounts (job_id, email, username, password,"
                " totp_secret, recovery_codes, status, created_at)"
                " VALUES (:job_id, :email, :username, :password,"
                " :totp_secret, :recovery_codes, :status, :created_at)",
                cols,
            )
            return int(cur.lastrowid)

    def _row_to_account(self, row: sqlite3.Row) -> Account:
        return Account(
            id=row["id"],
            job_id=row["job_id"],
            email=row["email"],
            username=row["username"],
            password=decrypt(row["password"]),
            totp_secret=decrypt(row["totp_secret"]),
            recovery_codes=decrypt(row["recovery_codes"]),
            status=row["status"],
            created_at=row["created_at"],
        )

    def get_by_email(self, email: str) -> Optional[Account]:
        row = self._conn().execute(
            "SELECT * FROM accounts WHERE email = ?", (email.strip().lower(),)
        ).fetchone()
        return self._row_to_account(row) if row else None

    def get(self, account_id: int) -> Optional[Account]:
        row = self._conn().execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        return self._row_to_account(row) if row else None

    def list(
        self,
        page: int = 1,
        per_page: int = 25,
        search: str = "",
        filter: str = "all",
    ) -> AccountPage:
        where, params = [], []
        if search.strip():
            where.append("(email LIKE ? OR username LIKE ?)")
            q = f"%{search.strip().lower()}%"
            params.extend([q, q])
        if filter == "has2fa":
            where.append("totp_secret != ''")
        elif filter == "no2fa":
            where.append("totp_secret = ''")
        elif filter == "recovery":
            where.append("recovery_codes != ''")
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        conn = self._conn()
        total = conn.execute(
            "SELECT COUNT(*) FROM accounts" + clause, params
        ).fetchone()[0]
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(max(1, page), pages)
        rows = conn.execute(
            "SELECT * FROM accounts" + clause
            + " ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()
        return AccountPage(
            rows=[self._row_to_account(r) for r in rows],
            total=total,
            page=page,
            per_page=per_page,
            pages=pages,
        )

    def delete(self, email: str) -> bool:
        conn = self._conn()
        with conn:
            cur = conn.execute(
                "DELETE FROM accounts WHERE email = ?", (email.strip().lower(),)
            )
            return cur.rowcount > 0

    def update_status(self, email: str, status: str) -> bool:
        conn = self._conn()
        with conn:
            cur = conn.execute(
                "UPDATE accounts SET status = ? WHERE email = ?",
                (status, email.strip().lower()),
            )
            return cur.rowcount > 0

    def count(self, filter: str = "all") -> int:
        cond = ""
        if filter == "has2fa":
            cond = " WHERE totp_secret != ''"
        elif filter == "no2fa":
            cond = " WHERE totp_secret = ''"
        elif filter == "recovery":
            cond = " WHERE recovery_codes != ''"
        return int(self._conn().execute("SELECT COUNT(*) FROM accounts" + cond).fetchone()[0])

    def daily_counts(self, days: int = 30) -> dict:
        """{YYYY-MM-DD: n} for the newest `days` days that have accounts."""
        rows = self._conn().execute(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n"
            " FROM accounts GROUP BY day ORDER BY day"
        ).fetchall()
        return {r["day"]: r["n"] for r in rows[-days:]}

    # ------------------------------------------------------------------ jobs

    def create(self, job: Job) -> int:
        conn = self._conn()
        with conn:
            cur = conn.execute(
                "INSERT INTO jobs (started_at, target, status, config_snapshot)"
                " VALUES (?, ?, 'running', ?)",
                (job.started_at or _now(), job.target, job.config_snapshot),
            )
            return int(cur.lastrowid)

    def finish(self, job_id: int, ok: int, fail: int, status: str, error: str = "") -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE jobs SET finished_at = ?, ok = ?, fail = ?, status = ?,"
                " error = ? WHERE id = ?",
                (_now(), ok, fail, status, error, job_id),
            )

    def latest(self) -> Optional[Job]:
        row = self._conn().execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return Job(
            id=row["id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            target=row["target"],
            ok=row["ok"],
            fail=row["fail"],
            status=row["status"],
            error=row["error"],
            config_snapshot=row["config_snapshot"],
        )

    def add_event(self, event: JobEvent) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "INSERT INTO job_events (job_id, ts, level, message)"
                " VALUES (?, ?, ?, ?)",
                (event.job_id, event.ts, event.level, event.message),
            )

    def events_after(self, job_id: int, after_id: int = 0, limit: int = 500) -> list[JobEvent]:
        rows = self._conn().execute(
            "SELECT * FROM job_events WHERE job_id = ? AND id > ?"
            " ORDER BY id LIMIT ?",
            (job_id, after_id, limit),
        ).fetchall()
        return [
            JobEvent(
                id=r["id"], job_id=r["job_id"], ts=r["ts"],
                level=r["level"], message=r["message"],
            )
            for r in rows
        ]

    # -------------------------------------------------------------- settings

    def get(self, key: str) -> Optional[SettingsEntry]:
        row = self._conn().execute(
            "SELECT * FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return None
        return SettingsEntry(
            key=row["key"],
            value=decrypt(row["value"]) if row["is_secret"] else row["value"],
            is_secret=bool(row["is_secret"]),
            updated_at=row["updated_at"],
        )

    def set(self, key: str, value: str, is_secret: bool = False) -> None:
        stored = encrypt(value) if is_secret else value
        conn = self._conn()
        with conn:
            conn.execute(
                "INSERT INTO settings (key, value, is_secret, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " is_secret = excluded.is_secret, updated_at = excluded.updated_at",
                (key, stored, int(is_secret), _now()),
            )

    def all(self) -> list[SettingsEntry]:
        rows = self._conn().execute("SELECT * FROM settings ORDER BY key").fetchall()
        return [
            SettingsEntry(
                key=r["key"],
                value=decrypt(r["value"]) if r["is_secret"] else r["value"],
                is_secret=bool(r["is_secret"]),
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    # -------------------------------------------------------- proxy blacklist

    def blacklist_add(self, ip: str) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "INSERT INTO proxy_blacklist (ip, blocked_at) VALUES (?, ?)"
                " ON CONFLICT(ip) DO UPDATE SET blocked_at = excluded.blocked_at",
                (ip, time.time()),
            )

    def blacklist_contains(self, ip: str) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM proxy_blacklist WHERE ip = ?", (ip,)
        ).fetchone()
        return row is not None

    def blacklist_purge_expired(self, ttl_sec: float = 3600 * 6) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "DELETE FROM proxy_blacklist WHERE blocked_at < ?",
                (time.time() - ttl_sec,),
            )

    # --------------------------------------------------------- trust cookies

    def save_trust(self, exit_ip: str, payload: str) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "INSERT INTO trust_cookies (id, exit_ip, payload, saved_at)"
                " VALUES (1, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET exit_ip = excluded.exit_ip,"
                " payload = excluded.payload, saved_at = excluded.saved_at",
                (exit_ip, payload, _now()),
            )

    def load_trust(self) -> Optional[tuple[str, str]]:
        row = self._conn().execute(
            "SELECT exit_ip, payload FROM trust_cookies WHERE id = 1"
        ).fetchone()
        return (row["exit_ip"], row["payload"]) if row else None

    # ------------------------------------------------- codebuddy accounts

    def get_next_for_codebuddy(self, account_id: Optional[int] = None) -> Optional[Account]:
        """Pick the next active GitHub account not yet registered on CodeBuddy.

        If account_id is given, pick that specific account (if available).
        Otherwise pick the next by id.
        """
        if account_id:
            row = self._conn().execute(
                "SELECT a.* FROM accounts a "
                "LEFT JOIN codebuddy_accounts ca ON ca.account_id = a.id "
                "WHERE a.id = ? AND a.status = 'active' AND ca.id IS NULL",
                (account_id,),
            ).fetchone()
        else:
            row = self._conn().execute(
                "SELECT a.* FROM accounts a "
                "LEFT JOIN codebuddy_accounts ca ON ca.account_id = a.id "
                "WHERE a.status = 'active' AND ca.id IS NULL "
                "ORDER BY a.id LIMIT 1"
            ).fetchone()
        return self._row_to_account(row) if row else None

    def list_available_for_codebuddy(self) -> list[dict]:
        """List all active accounts not yet registered on CodeBuddy."""
        rows = self._conn().execute(
            "SELECT a.id, a.email, a.username "
            "FROM accounts a "
            "LEFT JOIN codebuddy_accounts ca ON ca.account_id = a.id "
            "WHERE a.status = 'active' AND ca.id IS NULL "
            "ORDER BY a.id"
        ).fetchall()
        return [dict(r) for r in rows]

    def add_codebuddy_account(
        self, account_id: int, connection_id: int, region: str = ""
    ) -> int:
        conn = self._conn()
        with conn:
            cur = conn.execute(
                "INSERT INTO codebuddy_accounts "
                "(account_id, connection_id, region, status, created_at)"
                " VALUES (?, ?, ?, 'active', ?)",
                (account_id, connection_id, region, _now()),
            )
            return int(cur.lastrowid)

    def list_codebuddy_accounts(self) -> list[dict]:
        """List all CodeBuddy-registered accounts with GitHub details."""
        rows = self._conn().execute(
            "SELECT ca.id, ca.account_id, ca.connection_id, ca.region, ca.status,"
            " ca.created_at, a.email, a.username"
            " FROM codebuddy_accounts ca"
            " JOIN accounts a ON a.id = ca.account_id"
            " ORDER BY ca.id DESC"
        ).fetchall()
        return [
            {
                "id": r["id"],
                "account_id": r["account_id"],
                "connection_id": r["connection_id"],
                "region": r["region"],
                "status": r["status"],
                "created_at": r["created_at"],
                "email": r["email"],
                "username": r["username"],
            }
            for r in rows
        ]

    def count_codebuddy(self) -> int:
        return int(
            self._conn().execute("SELECT COUNT(*) FROM codebuddy_accounts").fetchone()[0]
        )
