"""Shared flow exceptions — kept dependency-free so any layer can raise/catch.

These live outside runner.py so browser/, net/, and future flow modules can
import them without circular dependencies.
"""
from __future__ import annotations


class SignupError(RuntimeError):
    pass


class SignupBlocked(SignupError):
    """DataDome-style hard block (not a rate limit)."""


class RegistrationCancelled(SignupError):
    """Stop requested via the web UI / Ctrl+C."""


class GitHubRateLimited(SignupError):
    """GitHub secondary rate limit."""
