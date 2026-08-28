"""Storage layer for accounts, jobs, and job events.

SQLite is the primary backend (WAL mode); the legacy "----" text format is
kept as an import/export codec for backward compatibility.
"""
