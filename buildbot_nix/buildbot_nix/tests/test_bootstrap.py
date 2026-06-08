"""Bootstrap unit tests: OIDC discovery retry and listener selection."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import FastAPI

from buildbot_nix.bootstrap import _serve, _uvicorn_configs, register_oidc_with_retry
from buildbot_nix.config import Config, OIDCConfig

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from buildbot_nix.auth import OAuthProvider


def make_config(state_dir: Path, **kwargs: Any) -> Config:
    return Config(
        db_url="postgresql://x",
        build_systems=["x86_64-linux"],
        url="http://ci.test",
        state_dir=state_dir,
        **kwargs,
    )


async def _app(scope: Any, receive: Any, send: Any) -> None:
    raise NotImplementedError


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


def test_unix_socket_skips_tcp_listener(tmp_path: Path) -> None:
    """With an nginx front over a unix socket, a 0.0.0.0 TCP listener
    is a plaintext bypass of the TLS proxy."""
    config = make_config(tmp_path, http_unix_socket=tmp_path / "web.sock")
    configs = _uvicorn_configs(config, app=_app)
    assert [c.uds for c in configs] == [str(tmp_path / "web.sock")]


def test_explicit_listen_flag_keeps_both(tmp_path: Path) -> None:
    config = make_config(
        tmp_path, http_unix_socket=tmp_path / "web.sock", http_listen=True
    )
    configs = _uvicorn_configs(config, app=_app)
    assert len(configs) == 2


def test_lifespan_runs_once_with_two_listeners(tmp_path: Path) -> None:
    """Two uvicorn servers share one app; each running the ASGI
    lifespan would start the app twice (duplicate LISTEN connection,
    double SSE event delivery)."""
    starts: list[str] = []
    stops: list[str] = []

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        starts.append("x")
        try:
            yield
        finally:
            stops.append("x")

    app = FastAPI(lifespan=lifespan)
    sock = tmp_path / "web.sock"
    config = make_config(tmp_path, http_unix_socket=sock, http_listen=True)
    config.http_port = 0  # ephemeral TCP port

    async def run() -> None:
        task = asyncio.create_task(_serve(app, _uvicorn_configs(config, app)))
        async with asyncio.timeout(10):
            while not sock.exists():  # noqa: ASYNC110 (poll fs, no event)
                await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert starts == ["x"]
    assert stops == ["x"]
