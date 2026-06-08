"""Bootstrap unit tests: OIDC discovery retry and listener selection."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx

from buildbot_nix.bootstrap import register_oidc_with_retry
from buildbot_nix.config import Config, OIDCConfig

if TYPE_CHECKING:
    from pathlib import Path

    from buildbot_nix.auth import OAuthProvider


def make_config(state_dir: Path, **kwargs: Any) -> Config:
    return Config(
        db_url="postgresql://x",
        build_systems=["x86_64-linux"],
        domain="ci.test",
        url="http://ci.test",
        state_dir=state_dir,
        **kwargs,
    )


DISCOVERY_DOC = {
    "issuer": "https://idp.example.com",
    "authorization_endpoint": "https://idp.example.com/auth",
    "token_endpoint": "https://idp.example.com/token",
    "userinfo_endpoint": "https://idp.example.com/userinfo",
}


def test_oidc_retry_registers_provider_after_transient_failure(
    tmp_path: Path,
) -> None:
    """A transient discovery failure at startup must not permanently
    disable OIDC login."""
    secret = tmp_path / "secret"
    secret.write_text("s3cret")
    oidc = OIDCConfig(
        name="IdP",
        discovery_url="https://idp.example.com/.well-known/openid-configuration",
        client_id="cid",
        scope=["openid"],
        client_secret_file=secret,
    )
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=DISCOVERY_DOC)

    providers: dict[str, OAuthProvider] = {}
    registered: list[str] = []
    asyncio.run(
        register_oidc_with_retry(
            oidc,
            providers,
            on_registered=lambda: registered.append("oidc"),
            retry_delay=0,
            http_factory=lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ),
        )
    )
    assert attempts == 3
    assert providers["oidc"].authorize_url == "https://idp.example.com/auth"
    assert registered == ["oidc"]
