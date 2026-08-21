"""Configuration loading for the GitHub register toolkit."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


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
    # post-signup stages (from user recording)
    create_repo: bool = True          # stage 4: create first repository
    repo_name: str = "hello"          # repo name prefix (username-suffix appended on conflict)
    enable_2fa: bool = True           # stage 5: enable TOTP 2FA and store the secret

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(
            f"config not found: {p} (copy config.example.json to config.json and fill it in)"
        )
    data = json.loads(p.read_text(encoding="utf-8"))
    return Config.from_dict(data)
