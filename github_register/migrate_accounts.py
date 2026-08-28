"""One-shot migration: legacy accounts/*.txt -> accounts/regkit.db.

Usage:
    python -m github_register.migrate_accounts            # migrate + report
    python -m github_register.migrate_accounts --db PATH # custom db path

Idempotent: re-running imports nothing new (email duplicates are skipped).
The legacy txt files are NOT deleted — they stay as a backup until you
remove them yourself.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .storage.legacy_txt import import_accounts_dir
from .storage.sqlite import SqliteStorage

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(prog="migrate-accounts")
    ap.add_argument("--accounts-dir", default=str(ROOT / "accounts"))
    ap.add_argument("--db", default=str(ROOT / "accounts" / "regkit.db"))
    args = ap.parse_args()

    accounts_dir = Path(args.accounts_dir)
    if not accounts_dir.is_dir():
        print(f"[!] accounts dir not found: {accounts_dir}")
        return 1

    storage = SqliteStorage(Path(args.db))
    report = import_accounts_dir(accounts_dir, storage, log=print)
    print()
    print(f"files scanned     : {report.files_scanned}")
    print(f"accounts imported : {report.accounts_imported}")
    print(f"duplicates skipped: {report.duplicates_skipped}")
    if report.errors:
        print("errors:")
        for err in report.errors:
            print(f"  - {err}")
        return 2
    print(f"total in db now   : {storage.count()}")
    print("[*] migration complete — legacy txt files left untouched as backup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
