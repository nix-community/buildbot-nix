"""Shared helpers for web tests."""

from __future__ import annotations


def cookie_header(cookies: dict[str, str]) -> dict[str, str]:
    """Cookies as a request header (per-request cookies= is
    deprecated in httpx)."""
    return {"cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}
