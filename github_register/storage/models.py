"""Data models for the storage layer."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Account:
    """One registered GitHub account with all its credentials.

    Sensitive fields (password, totp_secret, recovery_codes) are encrypted
    at the application layer before they reach any storage backend.
    """

    email: str
    username: str
    password: str
    totp_secret: str = ""
    recovery_codes: str = ""
    status: str = "active"  # active | disabled | invalid
    created_at: str = ""
    job_id: Optional[int] = None
    id: Optional[int] = None

    @classmethod
    def from_legacy_line(cls, line: str, created_at: str = "") -> Optional["Account"]:
        """Parse one 'email----password----username----totp----has_recovery' line.

        Tolerates the historical 2/3/4-field variants the same way the old
        web parser did. Returns None for lines that carry no credentials.
        """
        parts = [p.strip() for p in line.strip().split("----")]
        if len(parts) >= 4:
            email, password, username, totp = parts[0], parts[1], parts[2], parts[3]
        elif len(parts) == 3:
            email, password, username, totp = parts[0], parts[1], parts[2], ""
        elif len(parts) == 2:
            email, password, username, totp = parts[0], parts[1], parts[0].split("@")[0], ""
        else:
            return None
        if not email or not password:
            return None
        return cls(
            email=email,
            username=username or email.split("@")[0],
            password=password,
            totp_secret=totp,
            created_at=created_at,
        )

    def to_legacy_line(self) -> str:
        """Serialize back to the export format (recovery flag in field 5)."""
        return (
            f"{self.email}----{self.password}----{self.username}"
            f"----{self.totp_secret}----{int(bool(self.recovery_codes))}"
        )


@dataclass
class Job:
    """One registration job run (a batch of accounts)."""

    target: int = 0
    started_at: str = ""
    finished_at: Optional[str] = None
    ok: int = 0
    fail: int = 0
    status: str = "running"  # running | done | stopped | error
    error: str = ""
    config_snapshot: str = ""
    id: Optional[int] = None


@dataclass
class JobEvent:
    """One log line attached to a job."""

    job_id: int
    ts: str
    message: str
    level: str = "info"  # info | warn | error
    id: Optional[int] = None


@dataclass
class ProxyBlacklistEntry:
    ip: str
    blocked_at: float


@dataclass
class TrustCookie:
    """Persisted DataDome trust cookies bound to an exit IP."""

    exit_ip: str
    payload: str
    saved_at: str = ""
    id: int = 1  # singleton row


@dataclass
class SettingsEntry:
    key: str
    value: str
    is_secret: bool = False
    updated_at: str = ""


@dataclass
class AccountPage:
    """Paginated account query result."""

    rows: list = field(default_factory=list)
    total: int = 0
    page: int = 1
    per_page: int = 25
    pages: int = 1
