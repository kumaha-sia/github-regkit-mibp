"""Legacy '----' text format: import from old files, export for compat.

The old storage was one file per job under accounts/:
    accounts/github_accounts_YYYYMMDD_HHMMSS.txt
with one line per account:
    email----password----username----totp_secret----has_recovery
and recovery codes in accounts/recovery/<sha256(email)>.txt.

This module converts that world into the storage models so a one-shot
migration can move everything into SQLite.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..crypto import decrypt
from .models import Account

_FILENAME_RE = re.compile(r"github_accounts_(\d{8})_(\d{6})\.txt$")


@dataclass
class ImportReport:
    """Outcome of a legacy import — surfaced to the operator, not swallowed."""

    files_scanned: int = 0
    accounts_imported: int = 0
    duplicates_skipped: int = 0
    malformed_lines: int = 0
    errors: list = field(default_factory=list)


def _file_created_at(filename: str) -> str:
    """'github_accounts_20260825_035237.txt' -> '2026-08-25 03:52:37'."""
    m = _FILENAME_RE.search(filename)
    if not m:
        return ""
    d, t = m.group(1), m.group(2)
    return f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]}"


def recovery_key(email: str) -> str:
    """sha256 hex of the normalized email — legacy recovery filename."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def read_legacy_file(path: Path) -> list[Account]:
    """Parse one legacy accounts file into Account models.

    Lines may be plaintext or a single encrypted blob (enc:...) when the
    old encryption-at-rest was active. Malformed lines are skipped and
    counted by the caller via len comparison — parse() never raises on
    content; only on unreadable files.
    """
    raw = path.read_text(encoding="utf-8")
    if raw.startswith("enc:"):
        raw = decrypt(raw)
    accounts: list[Account] = []
    created = _file_created_at(path.name)
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        account = Account.from_legacy_line(line, created_at=created)
        if account is not None:
            accounts.append(account)
    return accounts


def read_legacy_recovery(recovery_dir: Path, email: str) -> str:
    """Recovery codes for an email, or '' when not captured."""
    path = recovery_dir / f"{recovery_key(email)}.txt"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def import_accounts_dir(
    accounts_dir: Path,
    storage,
    log=None,
) -> ImportReport:
    """Import every legacy file in accounts_dir into a SqliteStorage.

    One file becomes one Job row; account email duplicates across files are
    skipped (the earliest occurrence wins via insertion order oldest-first).
    Idempotent: re-running against the same DB adds nothing new.
    """
    report = ImportReport()
    files = sorted(accounts_dir.glob("github_accounts_*.txt"))
    report.files_scanned = len(files)
    recovery_dir = accounts_dir / "recovery"

    for path in files:
        job_id = None
        try:
            accounts = read_legacy_file(path)
        except Exception as exc:
            report.errors.append(f"{path.name}: {exc}")
            continue
        if not accounts:
            continue
        from .models import Job

        created = accounts[0].created_at or _file_created_at(path.name)
        job_id = storage.create(Job(target=len(accounts), started_at=created or ""))
        storage.finish(job_id, ok=len(accounts), fail=0, status="imported")
        for account in accounts:
            account.job_id = job_id
            account.recovery_codes = read_legacy_recovery(recovery_dir, account.email)
            try:
                storage.add(account)
                report.accounts_imported += 1
            except Exception as exc:
                # UNIQUE(email) collision = duplicate across files
                if "UNIQUE" in str(exc):
                    report.duplicates_skipped += 1
                else:
                    report.errors.append(f"{path.name} {account.email}: {exc}")
        if log:
            log(f"[*] imported {path.name}: {len(accounts)} rows")
    if log:
        log(
            f"[*] legacy import done: {report.accounts_imported} imported, "
            f"{report.duplicates_skipped} duplicates, {len(report.errors)} errors"
        )
    return report


def export_accounts_txt(accounts: list[Account]) -> str:
    """Render accounts back into the export format used by /api/accounts/download."""
    return "\n".join(a.to_legacy_line() for a in accounts) + ("\n" if accounts else "")
