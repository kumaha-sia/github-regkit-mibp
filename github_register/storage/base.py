"""Repository interfaces (Protocols) for the storage layer.

The runner and web server depend on these abstractions only, so the
backend can be SQLite, plain text, or a fake in tests.
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from .models import Account, AccountPage, Job, JobEvent, SettingsEntry


@runtime_checkable
class AccountRepository(Protocol):
    def add(self, account: Account) -> int:
        """Insert an account. Raises on unique (email/username) conflict."""
        ...

    def get_by_email(self, email: str) -> Optional[Account]:
        ...

    def get(self, account_id: int) -> Optional[Account]:
        ...

    def list(
        self,
        page: int = 1,
        per_page: int = 25,
        search: str = "",
        filter: str = "all",
    ) -> AccountPage:
        ...

    def delete(self, email: str) -> bool:
        ...

    def update_status(self, email: str, status: str) -> bool:
        ...

    def count(self, filter: str = "all") -> int:
        ...


@runtime_checkable
class JobRepository(Protocol):
    def create(self, job: Job) -> int:
        ...

    def finish(self, job_id: int, ok: int, fail: int, status: str, error: str = "") -> None:
        ...

    def latest(self) -> Optional[Job]:
        ...

    def add_event(self, event: JobEvent) -> None:
        ...

    def events_after(self, job_id: int, after_id: int = 0, limit: int = 500) -> list[JobEvent]:
        ...


@runtime_checkable
class SettingsRepository(Protocol):
    def get(self, key: str) -> Optional[SettingsEntry]:
        ...

    def set(self, key: str, value: str, is_secret: bool = False) -> None:
        ...

    def all(self) -> list[SettingsEntry]:
        ...
