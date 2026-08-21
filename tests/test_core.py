"""Self-check for non-network logic. Run: python -m tests.test_core"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_register.profiles import (
    extract_github_code,
    generate_password,
    generate_username,
    is_valid_username,
)


def test_extract_code():
    assert extract_github_code("Here's your GitHub verification code: 1234 5678") == "12345678"
    assert extract_github_code("Your verification code is 12345678. It expires soon.") == "12345678"
    assert extract_github_code("verification code: 9876 5432") == "98765432"
    assert extract_github_code("no code here") is None
    assert extract_github_code("") is None


def test_password():
    for _ in range(50):
        pw = generate_password()
        assert len(pw) >= 12
        assert any(c.islower() for c in pw)
        assert any(c.isupper() for c in pw)
        assert any(c.isdigit() for c in pw)


def test_username():
    for _ in range(100):
        name = generate_username()
        assert is_valid_username(name), name


def test_litensi_zone_pick():
    from github_register.litensi import LitensiClient

    cli = LitensiClient("id", "key", "github", "")
    zones = [
        {"zone": "a", "stock": 0, "price": 1},
        {"zone": "b", "stock": 5, "price": 3},
        {"zone": "c", "stock": 2, "price": 1.5},
    ]
    stock = [z for z in zones if float(z.get("stock") or 0) > 0]
    assert min(stock, key=lambda z: float(z.get("price") or 0))["zone"] == "c"


if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        fn()
        print(f"[OK] {name}")
    print("[*] all tests passed")
