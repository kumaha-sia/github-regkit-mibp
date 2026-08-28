"""Tests for runner persistence wiring. Run: python -m tests.test_runner"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import github_register.runner as runner
from github_register.config import Config
from github_register.storage.models import Account
from github_register.storage.sqlite import SqliteStorage


def test_finalize_account_runs_all_stages_once(monkeypatch=None):
    """Stage 4/5 pipeline must run repo -> 2FA -> recovery -> profile -> trust."""
    calls = []

    def fake_repo(page, username, repo_name, log):
        calls.append("repo")

    def fake_2fa(page, log):
        calls.append("2fa")
        return "TOTPSECRET", "r1\nr2"

    def fake_save_recovery(email, recovery, log):
        calls.append("recovery")

    def fake_profile(page, username, cfg, log):
        calls.append("profile")

    def fake_trust(context, log):
        calls.append("trust")

    orig = {
        "repo": runner._create_repository,
        "twofa": runner._enable_2fa,
        "save_recovery": runner._save_recovery_per_account,
        "profile": runner._complete_profile,
        "trust": runner._save_trust_cookie,
    }
    runner._create_repository = fake_repo
    runner._enable_2fa = fake_2fa
    runner._save_recovery_per_account = fake_save_recovery
    runner._complete_profile = fake_profile
    runner._save_trust_cookie = fake_trust
    try:
        cfg = Config(create_repo=True, enable_2fa=True, complete_profile=True)
        username, totp, recovery = runner._finalize_account(
            page=None, context=None, cfg=cfg,
            email="t@x.com", username="tuser", log=lambda m: None, stop=lambda: False,
        )
        assert calls == ["repo", "2fa", "recovery", "profile", "trust"]
        assert (username, totp, recovery) == ("tuser", "TOTPSECRET", "r1\nr2")

        # disabled stages skip cleanly (profile stage self-gates on blank fields)
        calls.clear()
        cfg_off = Config(create_repo=False, enable_2fa=False, complete_profile=False)
        u2, t2, r2 = runner._finalize_account(
            page=None, context=None, cfg=cfg_off,
            email="t@x.com", username="tuser", log=lambda m: None, stop=lambda: False,
        )
        assert calls == ["recovery", "profile", "trust"]
        assert (t2, r2) == ("", "")

        # stage failure never discards the account
        calls.clear()

        def boom(page, log):
            raise RuntimeError("2FA exploded")

        runner._enable_2fa = boom
        cfg_on = Config(create_repo=False, enable_2fa=True, complete_profile=False)
        u3, t3, r3 = runner._finalize_account(
            page=None, context=None, cfg=cfg_on,
            email="t@x.com", username="tuser", log=lambda m: None, stop=lambda: False,
        )
        assert calls == ["recovery", "profile", "trust"]
        assert (t3, r3) == ("", "")
    finally:
        runner._create_repository = orig["repo"]
        runner._enable_2fa = orig["twofa"]
        runner._save_recovery_per_account = orig["save_recovery"]
        runner._complete_profile = orig["profile"]
        runner._save_trust_cookie = orig["trust"]


def test_run_job_persists_to_sqlite(tmp_path=None):
    """run_job must write each successful registration into SQLite with a Job row."""
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "jobtest"
    tmp.mkdir(parents=True)
    runner.ACCOUNTS_DIR = tmp
    runner.DB_PATH = tmp / "regkit.db"
    runner.RECOVERY_DIR = tmp / "recovery"

    def fake_register_one(cfg, log, stop=None):
        return Account(
            email="job@x.com", username="jobuser", password="pw",
            totp_secret="SECRET", recovery_codes="rc1\nrc2",
        )

    orig = runner.register_one
    orig_stop_bridge = runner._stop_proxy_bridge
    runner.register_one = fake_register_one
    runner._stop_proxy_bridge = lambda: None
    try:
        cfg = Config(register_count=1, delay_sec=0)
        ok, fail, out = runner.run_job(cfg, log=lambda m: None)
        assert (ok, fail) == (1, 0)

        storage = SqliteStorage(runner.DB_PATH)
        job = storage.latest()
        assert job is not None and job.status == "done" and job.ok == 1
        stored = storage.get_by_email("job@x.com")
        assert stored is not None
        assert stored.password == "pw"
        assert stored.totp_secret == "SECRET"
        assert stored.recovery_codes == "rc1\nrc2"
        assert stored.job_id == job.id
        # legacy dual-write file exists too
        from github_register.crypto import decrypt
        content = out.read_text(encoding="utf-8")
        assert "job@x.com" in decrypt(content) or "job@x.com" in content
    finally:
        runner.register_one = orig
        runner._stop_proxy_bridge = orig_stop_bridge


if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        fn()
        print(f"[OK] {name}")
    print("[*] all runner tests passed")
