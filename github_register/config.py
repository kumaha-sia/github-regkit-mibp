"""Configuration loading for the GitHub register toolkit."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

from .crypto import encrypt, decrypt


# Fields that are encrypted at rest in config.json
SENSITIVE_FIELDS = {"litensi_api_id", "litensi_api_key", "proxy"}


@dataclass
class Config:
    litensi_api_id: str = ""
    litensi_api_key: str = ""
    litensi_site: str = "github.com"
    litensi_zone: str = ""
    register_count: int = 1
    proxy: str = ""
    headless: bool = False
    delay_sec: float = 5.0
    max_username_tries: int = 6
    otp_timeout_sec: int = 240
    browser_profile_dir: str = ".browser-profile"
    # fresh browser per account (incognito-like, zero cached state); the
    # DataDome trust cookie is carried over via .datadome-trust.json so the
    # signup page keeps loading without hard 403s
    fresh_profile: bool = True
    proxy_hard_block_retries: int = 2
    proxy_rate_limit_retries: int = 2
    rotate_ip_per_account: bool = True   # new sticky port (new IP) for each account
    # post-signup stages (from user recording)
    create_repo: bool = True          # stage 4: create first repository
    repo_name: str = "hello"          # repo name prefix (username-suffix appended on conflict)
    enable_2fa: bool = True           # stage 5: enable TOTP 2FA and store the secret
    set_profile_status: bool = True
    profile_status: str = "On vacation"  # blank disables custom status text
    complete_profile: bool = True
    profile_name: str = ""            # blank = Random User
    profile_bio: str = ""             # blank = ZenQuotes
    profile_location: str = ""        # blank = Random User country
    # notification webhook (Telegram/Discord/generic)
    notify_url: str = ""              # webhook URL; blank = disabled
    notify_token: str = ""            # optional bearer token or Telegram bot token

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        known = set(cls.__dataclass_fields__)
        d = {k: v for k, v in data.items() if k in known}
        # decrypt sensitive fields
        for field in SENSITIVE_FIELDS:
            if field in d and isinstance(d[field], str):
                d[field] = decrypt(d[field])
        return cls(**d)


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(
            f"config not found: {p} (copy config.example.json to config.json and fill it in)"
        )
    data = json.loads(p.read_text(encoding="utf-8"))
    return Config.from_dict(data)


def save_config(cfg: Config, path: str | Path) -> None:
    """Save config to JSON, encrypting sensitive fields if encryption is enabled."""
    p = Path(path)
    d = asdict(cfg)
    # encrypt sensitive fields
    for field in SENSITIVE_FIELDS:
        val = d.get(field, "")
        if val and isinstance(val, str):
            d[field] = encrypt(val)
    p.write_text(json.dumps(d, indent=4, ensure_ascii=False), encoding="utf-8")
