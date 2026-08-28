"""Tests for the SQLite storage layer. Run: python -m tests.test_storage"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import github_register.crypto as _crypto
from github_register.storage.legacy_txt import (
    export_accounts_txt,
    import_accounts_dir,
    recovery_key,
)
from github_register.storage.models import Account, Job, JobEvent
from github_register.storage.sqlite import SqliteStorage


def _tmp_storage():
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    return SqliteStorage(tmp), tmp


def _set_crypto_secret(secret: str | None) -> None:
    """Force a known encryption state (crypto caches its key)."""
    if secret is None:
        os.environ.pop("GITHUB_REGISTER_SECRET", None)
    else:
        os.environ["GITHUB_REGISTER_SECRET"] = secret
    _crypto._key_cache = _crypto._DISABLED


def test_account_roundtrip():
    s, _ = _tmp_storage()
    s.add(Account(email="a@x.com", username="usera", password="pw",
                  totp_secret="SECRET", recovery_codes="c1\nc2"))
    a = s.get_by_email("a@x.com")
    assert a is not None
    assert a.email == "a@x.com"
    assert a.password == "pw"
    assert a.totp_secret == "SECRET"
    assert a.recovery_codes == "c1\nc2"
    assert a.status == "active"


def test_account_unique_email():
    s, _ = _tmp_storage()
    s.add(Account(email="dup@x.com", username="u1", password="pw"))
    try:
        s.add(Account(email="dup@x.com", username="u2", password="pw"))
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("duplicate email must raise")


def test_list_pagination_search_filter():
    s, _ = _tmp_storage()
    s.add(Account(email="one@x.com", username="alpha", password="p", totp_secret="T"))
    s.add(Account(email="two@x.com", username="beta", password="p"))
    s.add(Account(email="three@x.com", username="gamma", password="p", totp_secret="T",
                  recovery_codes="rc"))
    page = s.list(page=1, per_page=2)
    assert page.total == 3
    assert page.pages == 2
    assert len(page.rows) == 2
    assert page.rows[0].email == "three@x.com"  # newest first

    searched = s.list(search="beta")
    assert searched.total == 1
    assert searched.rows[0].username == "beta"

    assert s.list(filter="has2fa").total == 2
    assert s.list(filter="no2fa").total == 1
    assert s.list(filter="recovery").total == 1
    assert s.count() == 3
    assert s.count("has2fa") == 2


def test_delete_and_status():
    s, _ = _tmp_storage()
    s.add(Account(email="gone@x.com", username="u", password="p"))
    assert s.delete("gone@x.com") is True
    assert s.get_by_email("gone@x.com") is None
    assert s.delete("gone@x.com") is False
    s.add(Account(email="keep@x.com", username="u2", password="p"))
    assert s.update_status("keep@x.com", "disabled") is True
    assert s.get_by_email("keep@x.com").status == "disabled"


def test_job_lifecycle_and_events():
    s, _ = _tmp_storage()
    jid = s.create(Job(target=5))
    assert jid > 0
    s.add_event(JobEvent(job_id=jid, ts="t1", message="start"))
    s.add_event(JobEvent(job_id=jid, ts="t2", message="mid", level="warn"))
    s.finish(jid, ok=3, fail=1, status="done", error="")
    j = s.latest()
    assert j.id == jid and j.status == "done" and j.ok == 3 and j.fail == 1
    assert j.finished_at  # timestamp written
    events = s.events_after(jid)
    assert len(events) == 2
    assert events[0].message == "start" and events[0].level == "info"
    assert events[1].message == "mid" and events[1].level == "warn"
    assert len(s.events_after(jid, after_id=events[0].id)) == 1


def test_settings_secret_storage():
    _set_crypto_secret("storage-test-secret")
    try:
        s, db = _tmp_storage()
        s.set("plain_key", "visible")
        s.set("api_key", "supersecret", is_secret=True)
        assert s.get("plain_key").value == "visible"
        assert s.get("api_key").value == "supersecret"
        assert s.get("api_key").is_secret is True
        assert s.get("missing") is None
        s.set("api_key", "rotated", is_secret=True)
        assert s.get("api_key").value == "rotated"
        # on disk it must NOT be plaintext
        raw = sqlite3.connect(str(db)).execute(
            "SELECT value FROM settings WHERE key='api_key'"
        ).fetchone()[0]
        assert raw.startswith("enc:") and "rotated" not in raw
    finally:
        _set_crypto_secret(None)


def test_sensible_columns_encrypted_at_rest():
    _set_crypto_secret("storage-test-secret")
    try:
        s, db = _tmp_storage()
        s.add(Account(email="enc@x.com", username="u", password="plainpw",
                      totp_secret="TOTPSECRET", recovery_codes="rc1"))
        row = sqlite3.connect(str(db)).execute(
            "SELECT password, totp_secret, recovery_codes FROM accounts WHERE email='enc@x.com'"
        ).fetchone()
        for stored, plain in zip(row, ("plainpw", "TOTPSECRET", "rc1")):
            assert stored.startswith("enc:") and plain not in stored
        # and the roundtrip still decrypts
        a = s.get_by_email("enc@x.com")
        assert (a.password, a.totp_secret, a.recovery_codes) == ("plainpw", "TOTPSECRET", "rc1")
    finally:
        _set_crypto_secret(None)


def test_trust_and_blacklist():
    s, _ = _tmp_storage()
    assert s.load_trust() is None
    s.save_trust("1.2.3.4", '{"cookies":[]}')
    assert s.load_trust() == ("1.2.3.4", '{"cookies":[]}')
    s.save_trust("5.6.7.8", "new")  # upsert singleton
    assert s.load_trust() == ("5.6.7.8", "new")

    assert s.blacklist_contains("9.9.9.9") is False
    s.blacklist_add("9.9.9.9")
    assert s.blacklist_contains("9.9.9.9") is True
    s.blacklist_purge_expired(ttl_sec=0)
    assert s.blacklist_contains("9.9.9.9") is False


def test_legacy_line_parsing():
    full = Account.from_legacy_line("e@x.com----pw----user----TOTP----1")
    assert (full.email, full.password, full.username, full.totp_secret) == \
        ("e@x.com", "pw", "user", "TOTP")
    three = Account.from_legacy_line("e@x.com----pw----user")
    assert three.totp_secret == ""
    two = Account.from_legacy_line("e@x.com----pw")
    assert two.username == "e"
    assert Account.from_legacy_line("garbage-no-separators") is None
    assert Account.from_legacy_line("") is None


def test_legacy_import_end_to_end():
    tmpdir = Path(tempfile.mkdtemp())
    accounts_dir = tmpdir / "accounts"
    recovery_dir = accounts_dir / "recovery"
    recovery_dir.mkdir(parents=True)
    (accounts_dir / "github_accounts_20260825_035237.txt").write_text(
        "one@x.com----pw1----user1----SECRET1----1\n"
        "two@x.com----pw2----user2----SECRET2----0\n",
        encoding="utf-8",
    )
    (accounts_dir / "github_accounts_20260826_075503.txt").write_text(
        "one@x.com----pw1----user1----SECRET1----1\n"   # duplicate email
        "three@x.com----pw3----user3\n",                  # 3-field variant
        encoding="utf-8",
    )
    (recovery_dir / f"{recovery_key('one@x.com')}.txt").write_text("r1\nr2\n", encoding="utf-8")

    s, _ = _tmp_storage()
    report = import_accounts_dir(accounts_dir, s)
    assert report.files_scanned == 2
    assert report.accounts_imported == 3
    assert report.duplicates_skipped == 1
    assert report.errors == []
    one = s.get_by_email("one@x.com")
    assert one.recovery_codes == "r1\nr2"
    assert one.created_at == "2026-08-25 03:52:37"
    three = s.get_by_email("three@x.com")
    assert three.totp_secret == "" and three.username == "user3"

    # idempotent second run: run1 had 3 inserts + 1 dup; run2 has 4 dups
    report2 = import_accounts_dir(accounts_dir, s)
    assert report2.accounts_imported == 0
    assert report2.duplicates_skipped == 4
    assert s.count() == 3


def test_export_format():
    accounts = [
        Account(email="a@x.com", username="ua", password="p", totp_secret="T",
                recovery_codes="rc"),
        Account(email="b@x.com", username="ub", password="p"),
    ]
    text = export_accounts_txt(accounts)
    lines = text.strip().splitlines()
    assert lines[0] == "a@x.com----p----ua----T----1"
    # empty totp field collapses to two adjacent separators
    assert lines[1] == "b@x.com----p----ub--------0"


def test_daily_counts():
    s, _ = _tmp_storage()
    a1 = Account(email="d1@x.com", username="u1", password="p", created_at="2026-08-25 10:00:00")
    a2 = Account(email="d2@x.com", username="u2", password="p", created_at="2026-08-25 11:00:00")
    a3 = Account(email="d3@x.com", username="u3", password="p", created_at="2026-08-26 09:00:00")
    for a in (a1, a2, a3):
        s.add(a)
    daily = s.daily_counts()
    assert daily == {"2026-08-25": 2, "2026-08-26": 1}


if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        fn()
        print(f"[OK] {name}")
    print("[*] all storage tests passed")
