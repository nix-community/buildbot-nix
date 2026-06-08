"""Control endpoint tests: authz gating, CSRF, admin toggle."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import asyncpg
import httpx
import pytest

from nixbot.api_tokens import ApiTokenStore
from nixbot.auth import AuthzConfig, User
from nixbot.bootstrap import build_service
from nixbot.config import Config
from nixbot.events import ChangeEvent, NullStatusReporter
from nixbot.forge_tokens import ForgeTokenStore
from nixbot.service import (
    MAX_REPORT_ATTEMPTS,
    RetryingReporter,
    _report_delay,
    repo_info,
)
from nixbot.status import StatusPostError, _raise_for_status
from nixbot.web.control_routes import (
    create_control_api_router,
    create_control_router,
)
from nixbot.web.token_routes import create_token_router

from .support import (
    WebHarness,
    insert_build,
    insert_project,
    web_harness,
)

pytestmark = pytest.mark.usefixtures("fresh_work_queue")

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from fastapi import FastAPI

AUTHZ = AuthzConfig(admins=["github:root"])

ROOT = User(provider="github", username="root")
ALICE = User(provider="github", username="alice")  # PR author
MALLORY = User(provider="gitea", username="alice")  # same name, wrong forge


@dataclass
class FakeBackend:
    restarted: list[int] = field(default_factory=list)
    attr_restarts: list[tuple[int, str]] = field(default_factory=list)
    effect_restarts: list[int] = field(default_factory=list)
    cancelled: list[int] = field(default_factory=list)
    attr_cancels: list[tuple[int, str]] = field(default_factory=list)
    refreshes: int = 0

    async def restart_build(self, build_id: int) -> None:
        self.restarted.append(build_id)

    async def restart_attribute(self, build_id: int, attr: str) -> None:
        self.attr_restarts.append((build_id, attr))

    async def restart_effects(self, build_id: int) -> None:
        self.effect_restarts.append(build_id)

    async def cancel_build(self, build_id: int) -> None:
        self.cancelled.append(build_id)

    async def cancel_attribute(self, build_id: int, attr: str) -> None:
        self.attr_cancels.append((build_id, attr))

    async def refresh_projects(self) -> None:
        self.refreshes += 1


@pytest.fixture(scope="module")
def postgres_dsn(postgres_dsn: str) -> str:
    asyncio.run(seed(postgres_dsn))
    return postgres_dsn


async def seed(dsn: str) -> None:
    pool = await asyncpg.create_pool(dsn)
    try:
        project_id = await insert_project(pool, forge_repo_id="ctl-1")
        build_id = await insert_build(
            pool,
            project_id,
            status="failed",
            pr_number=7,
            pr_author="github:alice",
        )
        await pool.execute(
            """
            INSERT INTO build_attributes (build_id, attr, system, status) VALUES
              ($1, 'good', 'x', 'succeeded'),
              ($1, 'bad1', 'x', 'failed'),
              ($1, 'bad2', 'x', 'dependency_failed'),
              ($1, 'pkgs/o k?#100%', 'x', 'succeeded')
            """,
            build_id,
        )
        # Queue for the mass-cancel tests: two pending, one running.
        queued_project = await insert_project(
            pool, name="qwidget", forge_repo_id="ctl-q"
        )
        await insert_build(pool, queued_project, number=1, status="pending")
        await insert_build(pool, queued_project, number=2, status="pending")
        await insert_build(
            pool, queued_project, number=3, status="building", started=True
        )
    finally:
        await pool.close()


BACKEND = FakeBackend()


@pytest.fixture(scope="module")
def harness(postgres_dsn: str) -> Iterator[WebHarness]:
    def configure(app: FastAPI, pool: asyncpg.Pool) -> None:
        ctx = app.state.web_context
        ctx.authz = AUTHZ
        app.include_router(create_control_router(ctx, BACKEND, AUTHZ, "http://test"))
        app.include_router(
            create_control_api_router(ctx, BACKEND, AUTHZ, "http://test")
        )
        ctx.token_store = ApiTokenStore(pool)
        app.include_router(create_token_router(ctx, ctx.token_store, "http://test"))

    with web_harness(postgres_dsn, configure=configure) as h:
        yield h


def test_control_buttons_hidden_without_permission(harness: WebHarness) -> None:
    url = "/repos/github/acme/widget/builds/1"
    assert "rebuild failed" not in harness.get(url).text
    assert "rebuild failed" not in harness.get(url, MALLORY).text
    assert "rebuild failed" in harness.get(url, ROOT).text
    assert "rebuild failed" in harness.get(url, ALICE).text


def test_anonymous_cannot_control(harness: WebHarness) -> None:
    response = harness.post("/repos/github/acme/widget/builds/1/restart")
    assert response.status_code == 403
    assert BACKEND.restarted == []


def test_admin_can_restart_and_cancel(harness: WebHarness) -> None:
    assert (
        harness.post("/repos/github/acme/widget/builds/1/restart", ROOT).status_code
        == 303
    )
    assert len(BACKEND.restarted) == 1
    assert (
        harness.post("/repos/github/acme/widget/builds/1/cancel", ROOT).status_code
        == 303
    )
    assert len(BACKEND.cancelled) == 1
    assert (
        harness.post(
            "/repos/github/acme/widget/builds/1/attrs/x86_64-linux.ok/cancel",
            ROOT,
        ).status_code
        == 303
    )
    assert BACKEND.attr_cancels == [(1, "x86_64-linux.ok")]


def test_pr_author_can_control_own_pr(harness: WebHarness) -> None:
    assert (
        harness.post("/repos/github/acme/widget/builds/1/restart", ALICE).status_code
        == 303
    )
    # Same username on another forge: rejected.
    assert (
        harness.post("/repos/github/acme/widget/builds/1/restart", MALLORY).status_code
        == 403
    )


def test_cancel_pending_requires_admin(harness: WebHarness) -> None:
    BACKEND.cancelled.clear()
    assert harness.post("/builds/queue/cancel-pending").status_code == 403
    assert harness.post("/builds/queue/cancel-pending", ALICE).status_code == 403
    assert BACKEND.cancelled == []


def test_cancel_pending_cancels_only_queued_builds(harness: WebHarness) -> None:
    BACKEND.cancelled.clear()
    response = harness.post("/builds/queue/cancel-pending", ROOT)
    assert response.status_code == 303
    assert response.headers["location"] == "/builds"
    pending = harness.run(
        harness.ctx.pool.fetch("SELECT id FROM builds WHERE status = 'pending'")
    )
    assert sorted(BACKEND.cancelled) == sorted(r["id"] for r in pending)
    assert len(BACKEND.cancelled) == 2


def test_cancel_pending_button_admin_only(harness: WebHarness) -> None:
    assert "cancel queued" in harness.get("/builds", ROOT).text
    assert "cancel queued" not in harness.get("/builds", ALICE).text
    assert "cancel queued" not in harness.get("/builds").text


def test_csrf_cross_origin_rejected(harness: WebHarness) -> None:
    response = harness.post(
        "/repos/github/acme/widget/builds/1/restart",
        ROOT,
        origin="https://evil.example.com",
    )
    assert response.status_code == 403


def test_restart_single_attribute(harness: WebHarness) -> None:
    BACKEND.attr_restarts.clear()
    assert (
        harness.post(
            "/repos/github/acme/widget/builds/1/attrs/bad1/restart", ROOT
        ).status_code
        == 303
    )
    assert [a for _, a in BACKEND.attr_restarts] == ["bad1"]


def test_restart_unknown_attribute_is_404(harness: WebHarness) -> None:
    BACKEND.attr_restarts.clear()
    response = harness.post(
        "/repos/github/acme/widget/builds/1/attrs/ghost/restart", ROOT
    )
    assert response.status_code == 404
    assert BACKEND.attr_restarts == []


def test_restart_attribute_with_special_chars_redirects_encoded(
    harness: WebHarness,
) -> None:
    """Attrs containing / % ? # must match the route and come back
    percent-encoded in the redirect Location header."""
    BACKEND.attr_restarts.clear()
    attr = "pkgs/o k?#100%"
    encoded = "pkgs/o%20k%3F%23100%25"
    response = harness.post(
        f"/repos/github/acme/widget/builds/1/attrs/{encoded}/restart", ROOT
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/logs/{encoded}")
    assert [a for _, a in BACKEND.attr_restarts] == [attr]


def test_rebuild_all_failed(harness: WebHarness) -> None:
    BACKEND.attr_restarts.clear()
    assert (
        harness.post(
            "/repos/github/acme/widget/builds/1/rebuild-failed", ROOT
        ).status_code
        == 303
    )
    assert sorted(a for _, a in BACKEND.attr_restarts) == ["bad1", "bad2"]


def project_enabled(harness: WebHarness) -> bool:
    pool = harness.ctx.pool
    return harness.run(
        pool.fetchval("SELECT enabled FROM projects WHERE forge_repo_id = 'ctl-1'")
    )


def test_admin_project_toggle(harness: WebHarness) -> None:
    # Non-admin rejected.
    assert harness.post("/admin/repos/1/toggle", ALICE).status_code == 403

    before = project_enabled(harness)
    assert harness.post("/admin/repos/1/toggle", ROOT).status_code == 303
    assert project_enabled(harness) != before

    # The dashboard filter survives a toggle.
    response = harness.post("/admin/repos/1/toggle", ROOT, data={"q": "wid get"})
    assert response.status_code == 303
    assert response.headers["location"] == "/?q=wid%20get"


def test_repo_refresh(harness: WebHarness) -> None:
    # Anonymous rejected: discovery hits forge APIs.
    assert harness.post("/admin/repos/refresh").status_code == 403
    assert BACKEND.refreshes == 0

    # Any logged-in user may refresh, not only admins.
    response = harness.post("/admin/repos/refresh", ALICE, data={"q": "wid get"})
    assert response.status_code == 303
    assert response.headers["location"] == "/?q=wid%20get"
    assert BACKEND.refreshes == 1

    # Cross-origin rejected.
    assert (
        harness.post("/admin/repos/refresh", ROOT, origin="http://evil").status_code
        == 403
    )
    assert BACKEND.refreshes == 1


def test_api_enable_disable(harness: WebHarness) -> None:
    assert (
        harness.post("/api/repos/github/acme/widget/disable", ALICE).status_code == 403
    )

    response = harness.post("/api/repos/github/acme/widget/disable", ROOT)
    assert response.status_code == 200
    assert response.json() == {"owner": "acme", "name": "widget", "enabled": False}
    assert project_enabled(harness) is False
    # Idempotent: repeating is fine.
    assert (
        harness.post("/api/repos/github/acme/widget/disable", ROOT).status_code == 200
    )
    assert harness.post("/api/repos/github/acme/widget/enable", ROOT).status_code == 200
    assert project_enabled(harness) is True
    assert harness.post("/api/repos/github/acme/nope/enable", ROOT).status_code == 404


def test_api_restart_and_cancel(harness: WebHarness) -> None:
    BACKEND.restarted.clear()
    BACKEND.cancelled.clear()

    assert (
        harness.post("/api/repos/github/acme/widget/builds/1/restart").status_code
        == 403
    )
    assert BACKEND.restarted == []

    response = harness.post("/api/repos/github/acme/widget/builds/1/restart", ROOT)
    assert response.status_code == 200
    assert response.json() == {"number": 1, "action": "restart"}
    assert len(BACKEND.restarted) == 1

    response = harness.post("/api/repos/github/acme/widget/builds/1/cancel", ROOT)
    assert response.status_code == 200
    assert response.json() == {"number": 1, "action": "cancel"}
    assert len(BACKEND.cancelled) == 1

    assert (
        harness.post(
            "/api/repos/github/acme/widget/builds/99/restart", ROOT
        ).status_code
        == 404
    )


# --- personal API tokens ---------------------------------------------


def test_api_token_lifecycle(harness: WebHarness) -> None:
    pool = harness.ctx.pool
    store = ApiTokenStore(pool)

    async def run() -> None:
        token = await store.create(ALICE, "laptop")
        assert token.startswith("bnix_")
        # Stored only as hash.
        stored = await pool.fetchval(
            "SELECT token_hash FROM api_tokens WHERE name = 'laptop'"
        )
        assert token not in stored
        # Authenticates as owner.
        assert await store.authenticate(token) == ALICE
        assert await store.authenticate("bnix_wrong") is None
        assert await store.authenticate("malformed") is None
        # Expired token rejected.
        expired = await store.create(
            ALICE, "old", expires_at=datetime.now(tz=UTC) - timedelta(days=1)
        )
        assert await store.authenticate(expired) is None
        # Revocation: immediate, owner-only.
        tokens = await store.list_for(ALICE)
        laptop = next(t for t in tokens if t.name == "laptop")
        assert not await store.revoke(ROOT, laptop.id)  # not the owner
        assert await store.revoke(ALICE, laptop.id)
        assert await store.authenticate(token) is None

    harness.run(run())


def test_expired_api_tokens_pruned(harness: WebHarness) -> None:
    """Expired tokens are deleted, not kept forever cluttering
    /settings and the api_tokens table."""
    pool = harness.ctx.pool
    store = ApiTokenStore(pool)

    async def run() -> None:
        stale = await store.create(
            ALICE, "stale", expires_at=datetime.now(tz=UTC) - timedelta(days=1)
        )
        assert await store.authenticate(stale) is None
        assert all(t.name != "stale" for t in await store.list_for(ALICE))
        count = await pool.fetchval(
            "SELECT count(*) FROM api_tokens WHERE name = 'stale'"
        )
        assert count == 0

    harness.run(run())


def test_token_creation_with_expiry(harness: WebHarness) -> None:
    def create(data: dict[str, str]) -> httpx.Response:
        return harness.post("/settings/tokens", ALICE, data=data)

    assert create({"name": "ci", "expires_days": "7"}).status_code == 200
    store = harness.ctx.token_store
    assert store is not None
    tokens = harness.run(store.list_for(ALICE))
    ci = next(t for t in tokens if t.name == "ci")
    assert ci.expires_at is not None
    delta = ci.expires_at - datetime.now(tz=UTC)
    assert timedelta(days=6) < delta <= timedelta(days=7)

    # Expiry stays optional.
    assert create({"name": "forever"}).status_code == 200
    tokens = harness.run(store.list_for(ALICE))
    assert next(t for t in tokens if t.name == "forever").expires_at is None

    # Invalid values rejected.
    assert create({"name": "bad", "expires_days": "x"}).status_code == 400
    assert create({"name": "bad", "expires_days": "-1"}).status_code == 400
    assert create({"name": "bad", "expires_days": "999999999999"}).status_code == 400


def test_forge_token_store(harness: WebHarness) -> None:
    pool = harness.ctx.pool
    store = ForgeTokenStore(pool)
    harness.run(store.save("sid-1", "tok", 3600))
    assert harness.run(store.get("sid-1")) == "tok"
    harness.run(store.delete("sid-1"))
    assert harness.run(store.get("sid-1")) is None
    # Expired tokens are not returned.
    harness.run(store.save("sid-2", "tok2", -1))
    assert harness.run(store.get("sid-2")) is None


def test_api_token_controls_build(harness: WebHarness) -> None:
    ctx = harness.ctx
    store = ApiTokenStore(ctx.pool)
    ctx.token_store = store

    token = harness.run(store.create(ALICE, "api"))
    BACKEND.restarted.clear()
    response = harness.post(
        "/repos/github/acme/widget/builds/1/restart",
        origin="",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 303  # PR author via token
    assert len(BACKEND.restarted) == 1

    bad = harness.post(
        "/repos/github/acme/widget/builds/1/restart",
        origin="",
        headers={"Authorization": "Bearer bnix_invalid"},
    )
    assert bad.status_code == 403


# --- service composition smoke test -----------------------------------


def test_build_service_composition(postgres_dsn: str, tmp_path: Path) -> None:
    async def run() -> None:
        config = Config(
            db_url=postgres_dsn,
            build_systems=["x86_64-linux"],
            url="http://ci.test",
            state_dir=tmp_path / "state",
        )
        service, app = await build_service(config)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://ci.test"
            ) as client:
                assert (await client.get("/health")).text == "ok"
                assert (await client.get("/")).status_code == 200
                assert (await client.get("/metrics")).status_code == 200
                # Webhooks 404 without forge configuration.
                assert (
                    await client.post("/webhooks/github", content=b"{}")
                ).status_code == 404
                # Control endpoints present and gated.
                assert (
                    await client.post("/repos/github/acme/widget/builds/1/cancel")
                ).status_code == 403
        finally:
            await service.pool.close()

    asyncio.run(run())


def test_restart_clears_failed_cache_and_guards_running(
    postgres_dsn: str, tmp_path: Path
) -> None:
    async def run() -> None:
        config = Config(
            db_url=postgres_dsn,
            build_systems=["x86_64-linux"],
            url="http://ci.test",
            state_dir=tmp_path / "state",
        )
        service, _app = await build_service(config)
        pool = service.pool
        try:
            project_id = await insert_project(pool, forge_repo_id="77", url="http://x")
            build_id = await insert_build(
                pool, project_id, commit_sha="c1", tree_hash="t1", status="failed"
            )
            await pool.execute(
                "INSERT INTO build_attributes (build_id, attr, system, drv_path, "
                "status) VALUES ($1, 'x86_64-linux.a', 'x86_64-linux', "
                "'/nix/store/a.drv', 'failed')",
                build_id,
            )
            await pool.execute(
                "INSERT INTO failed_builds (project_id, derivation, timestamp, url) "
                "VALUES ($1, '/nix/store/a.drv', 1, 'http://old')",
                project_id,
            )

            # Running build: restart refuses, cache row stays.
            service.orchestrator.cancel_events[build_id] = asyncio.Event()
            await service.restart_build(build_id)
            await service.drain_work()
            assert await pool.fetchval("SELECT count(*) FROM failed_builds") == 1
            assert (
                await pool.fetchval(
                    "SELECT status FROM build_attributes WHERE build_id = $1",
                    build_id,
                )
                == "failed"
            )

            # Not running: cache cleared, attributes reset.
            del service.orchestrator.cancel_events[build_id]
            await service.restart_build(build_id)
            await service.drain_work()
            assert await pool.fetchval("SELECT count(*) FROM failed_builds") == 0
            assert (
                await pool.fetchval(
                    "SELECT status FROM build_attributes WHERE build_id = $1",
                    build_id,
                )
                == "pending"
            )
        finally:
            await pool.close()

    asyncio.run(run())


def test_webhook_setup_shown_to_admin_only(harness: WebHarness) -> None:
    async def seed() -> None:
        ctx = harness.ctx
        project_id = await insert_project(
            ctx.pool, "locked", forge="gitea", forge_repo_id="wh-1"
        )
        await ctx.pool.execute(
            "INSERT INTO webhook_secrets (project_id, secret)"
            " VALUES ($1, 's3cret') ON CONFLICT (project_id) DO NOTHING",
            project_id,
        )
        ctx.webhook_base_url = "https://ci.example.com"

    harness.run(seed())
    url = "/repos/gitea/acme/locked"
    admin = harness.get(url, ROOT).text
    assert "/webhooks/gitea" in admin
    # The secret is never readable, only rotated and shown once.
    assert "s3cret" not in admin
    assert "regenerate" in admin
    anonymous = harness.get(url).text
    assert "/webhooks/gitea" not in anonymous


def test_webhook_secret_regeneration(harness: WebHarness) -> None:
    async def project_id() -> int:
        ctx = harness.ctx
        return await ctx.pool.fetchval(
            "SELECT id FROM projects WHERE forge_repo_id = 'wh-1'"
        )

    pid = harness.run(project_id())
    url = f"/admin/repos/{pid}/webhook-secret"
    assert harness.post(url, ALICE).status_code == 403
    assert harness.post(url, ROOT, origin="https://evil.example.com").status_code == 403
    rotated = harness.post(url, ROOT)
    assert rotated.status_code == 200
    assert "s3cret" not in rotated.text  # old secret replaced

    async def stored() -> str:
        ctx = harness.ctx
        return await ctx.pool.fetchval(
            "SELECT secret FROM webhook_secrets WHERE project_id = $1", pid
        )

    new_secret = harness.run(stored())
    assert new_secret != "s3cret"  # noqa: S105 (seeded test value)
    assert new_secret in rotated.text  # shown exactly here, once


def test_retry_after_parsing() -> None:
    response = httpx.Response(429, headers={"Retry-After": "120"})
    with pytest.raises(StatusPostError) as exc:
        _raise_for_status(response, "acme/widget")
    assert exc.value.retry_after == 120

    # GitHub primary rate limit: reset epoch instead of Retry-After.
    reset = int(time.time()) + 300
    response = httpx.Response(
        403,
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)},
    )
    with pytest.raises(StatusPostError) as exc:
        _raise_for_status(response, "acme/widget")
    assert exc.value.retry_after is not None
    assert 295 <= exc.value.retry_after <= 300

    # Garbage hints must not poison the delay math.
    for bogus in ("nan", "inf", "soon"):
        response = httpx.Response(429, headers={"Retry-After": bogus})
        with pytest.raises(StatusPostError) as exc:
            _raise_for_status(response, "acme/widget")
        assert exc.value.retry_after is None or math.isfinite(exc.value.retry_after)

    _raise_for_status(httpx.Response(201), "acme/widget")  # must not raise


def test_report_delay_honors_retry_after() -> None:
    # The forge hint dominates the attempt backoff and is capped.
    assert _report_delay(1, None) == 0
    assert _report_delay(2, None) == 30
    assert _report_delay(2, time.time() + 240) == pytest.approx(240, abs=2)
    assert _report_delay(1, time.time() + 7200) == 3600


def test_report_retry(
    postgres_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed terminal status post is retried from database state and
    gives up after MAX_REPORT_ATTEMPTS instead of looping."""
    monkeypatch.setattr("nixbot.service.REPORT_BACKOFF_SECONDS", 0)

    async def run() -> None:
        config = Config(
            db_url=postgres_dsn,
            build_systems=["x86_64-linux"],
            url="http://ci.test",
            state_dir=tmp_path / "state",
        )
        service, _app = await build_service(config)
        pool = service.pool
        try:
            project_id = await insert_project(pool, forge_repo_id="80", url="http://x")
            build_id = await insert_build(
                pool, project_id, commit_sha="c9", tree_hash="t9", status="succeeded"
            )
            await pool.execute(
                "INSERT INTO build_attributes (build_id, attr, status) "
                "VALUES ($1, 'x86_64-linux.a', 'succeeded')",
                build_id,
            )

            posts: list[dict] = []
            fail = True

            class FakeReporter(NullStatusReporter):
                async def build_finished(  # noqa: PLR0913
                    self,
                    event: ChangeEvent,
                    build: Any,
                    status: str,
                    generation: int,
                    results: Any,
                    *,
                    attr_statuses: dict[str, str] | None = None,
                    attr_prefix: str = "checks",
                ) -> None:
                    del event, status, generation, results, attr_prefix
                    posts.append({"build": build.id, "attrs": attr_statuses})
                    if fail:
                        msg = "forge 502"
                        raise RuntimeError(msg)

            wrapper = RetryingReporter(FakeReporter(), service)
            service.orchestrator.reporter = wrapper

            build = await service.orchestrator.db.get_build(build_id)
            assert build is not None
            record = await service.repo_store.by_id(project_id)
            assert record is not None
            event = ChangeEvent(repo=repo_info(record), branch="main", commit_sha="c9")
            # Inline post fails: the wrapper swallows and queues.
            await wrapper.build_finished(event, build, "succeeded", 0, [])
            fail = False
            await service.drain_work()
            assert [p["build"] for p in posts] == [build_id, build_id]
            # The retry re-derives attribute state from the database.
            assert posts[-1]["attrs"] == {"x86_64-linux.a": "succeeded"}

            # Permanent failure: attempts are bounded.
            posts.clear()
            fail = True
            await wrapper.build_finished(event, build, "succeeded", 0, [])
            await service.drain_work()
            assert len(posts) == MAX_REPORT_ATTEMPTS + 1  # inline + retries
            failed = await pool.fetchval(
                "SELECT count(*) FROM work_queue "
                "WHERE kind = 'report' AND status = 'failed'"
            )
            assert failed == MAX_REPORT_ATTEMPTS
        finally:
            await pool.close()

    asyncio.run(run())


def test_effect_item_setup_failure_settles_row(
    postgres_dsn: str, tmp_path: Path
) -> None:
    """A fetch/checkout failure in an effect item must not leave the
    row pending forever."""

    async def run() -> None:
        config = Config(
            db_url=postgres_dsn,
            build_systems=["x86_64-linux"],
            url="http://ci.test",
            state_dir=tmp_path / "state",
        )
        service, _app = await build_service(config)
        pool = service.pool
        try:
            project_id = await insert_project(
                pool, "gear", forge_repo_id="81", url="file:///nonexistent"
            )
            build_id = await insert_build(
                pool, project_id, commit_sha="c5", tree_hash="t5", status="succeeded"
            )
            await pool.execute(
                "INSERT INTO build_effects (build_id, name, status) "
                "VALUES ($1, 'deploy', 'pending')",
                build_id,
            )
            await service.enqueue_work(
                "effect", f"build-{build_id}", {"build_id": build_id, "name": "deploy"}
            )
            await service.drain_work()
            row = await pool.fetchrow(
                "SELECT status, error FROM build_effects WHERE build_id = $1",
                build_id,
            )
            assert row["status"] == "failed"
            assert row["error"]
            item = await pool.fetchrow(
                "SELECT status FROM work_queue WHERE kind = 'effect'"
            )
            assert item["status"] == "failed"
        finally:
            await pool.close()

    asyncio.run(run())


def test_work_dispatch(postgres_dsn: str, tmp_path: Path) -> None:
    """The dispatcher reconstructs payloads and fails unknown kinds."""

    async def run() -> None:
        config = Config(
            db_url=postgres_dsn,
            build_systems=["x86_64-linux"],
            url="http://ci.test",
            state_dir=tmp_path / "state",
        )
        service, _app = await build_service(config)
        pool = service.pool
        try:
            # Scheduled payload roundtrip; the unknown project makes
            # execution a no-op, but parsing must succeed.
            await service.enqueue_work(
                "scheduled",
                "scheduled-999-s-e",
                {
                    "project_id": 999,
                    "schedule_name": "s",
                    "effect": "e",
                    "when": {"minute": 5, "dayOfWeek": ["Mon"]},
                },
            )
            await service.enqueue_work(
                "refresh-schedules", "schedules-999", {"project_id": 999, "rev": "x"}
            )
            await service.enqueue_work("bogus", "k", {})
            await service.drain_work()
            rows = await pool.fetch(
                "SELECT kind, status, error FROM work_queue ORDER BY id"
            )
            assert [(r["kind"], r["status"]) for r in rows] == [
                ("scheduled", "done"),
                ("refresh-schedules", "done"),
                ("bogus", "failed"),
            ]
            assert "unknown work kind" in rows[2]["error"]
        finally:
            await pool.close()

    asyncio.run(run())


def test_restart_resets_effects(postgres_dsn: str, tmp_path: Path) -> None:
    """A full restart re-runs effects: clear the started-flag and the
    previous run's rows, or the page keeps showing stale results."""

    async def run() -> None:
        config = Config(
            db_url=postgres_dsn,
            build_systems=["x86_64-linux"],
            url="http://ci.test",
            state_dir=tmp_path / "state",
        )
        service, _app = await build_service(config)
        pool = service.pool
        try:
            project_id = await insert_project(
                pool, "sprocket", forge_repo_id="79", url="http://x"
            )
            build_id = await insert_build(
                pool,
                project_id,
                commit_sha="c1",
                tree_hash="t1",
                status="failed",
                effects_started=True,
            )
            await pool.execute(
                "INSERT INTO build_effects (build_id, name, status, finished_at, "
                "log_path) VALUES ($1, 'deploy', 'failed', now(), 'old.zst')",
                build_id,
            )

            await service.restart_build(build_id)
            await service.drain_work()
            assert not await pool.fetchval(
                "SELECT effects_started FROM builds WHERE id = $1", build_id
            )
            # Stale log cleared by the reset; the failed rerun
            # (unfetchable URL) settled the row.
            row = await pool.fetchrow(
                "SELECT status, error, log_path FROM build_effects WHERE build_id = $1",
                build_id,
            )
            assert dict(row) == {
                "status": "failed",
                "error": "build did not succeed",
                "log_path": None,
            }

            # Single-attribute restart keeps the guard: a partial
            # rebuild must not re-deploy.
            await pool.execute(
                "UPDATE builds SET effects_started = TRUE WHERE id = $1", build_id
            )
            await service.restart_attribute(build_id, "x86_64-linux.a")
            await service.drain_work()
            assert await pool.fetchval(
                "SELECT effects_started FROM builds WHERE id = $1", build_id
            )
        finally:
            await pool.close()

    asyncio.run(run())


def test_effects_restart_route(harness: WebHarness) -> None:
    url = "/repos/github/acme/widget/builds/1/effects/restart"
    assert harness.post(url).status_code == 403
    assert harness.post(url, ROOT).status_code == 303
    assert BACKEND.effect_restarts == [1]


def test_webhook_panel_is_forge_aware(harness: WebHarness) -> None:
    """The manual webhook instructions must match the project's forge:
    GitLab has no Gitea-style event names or content-type setting."""

    async def seed() -> None:
        await insert_project(
            harness.ctx.pool,
            "lab",
            forge="gitlab",
            forge_repo_id="hook-gl1",
            url="https://gitlab.example.com/acme/lab.git",
        )
        await insert_project(
            harness.ctx.pool,
            "tea",
            forge="gitea",
            forge_repo_id="hook-gt1",
            url="https://gitea.example.com/acme/tea.git",
        )

    harness.loop.run_until_complete(seed())
    harness.ctx.webhook_base_url = "https://ci.example.com"
    try:
        gitlab_page = harness.get("/repos/gitlab/acme/lab", ROOT).text
        assert "Merge request events" in gitlab_page
        assert "pull_request_sync" not in gitlab_page
        assert "/webhooks/gitlab" in gitlab_page

        gitea_page = harness.get("/repos/gitea/acme/tea", ROOT).text
        assert "pull_request_sync" in gitea_page
        assert "Merge request events" not in gitea_page

        # GitHub hooks come from the app subscription: no panel.
        github_page = harness.get("/repos/github/acme/widget", ROOT).text
        assert "webhook setup" not in github_page
    finally:
        harness.ctx.webhook_base_url = None
