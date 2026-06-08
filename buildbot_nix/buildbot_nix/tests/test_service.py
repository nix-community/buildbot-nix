"""Service composition regression tests (service.py)."""

# ruff: noqa: PLR2004, ARG001, ARG002 (stub callbacks ignore arguments)

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from buildbot_nix.bootstrap import _startup, build_service, run_service
from buildbot_nix.config import (
    Config,
    PullBasedConfig,
    PullBasedRepository,
    resolve_credential_path,
)
from buildbot_nix.events import NullStatusReporter
from buildbot_nix.forge import DiscoveredRepo, GitlabClient
from buildbot_nix.scheduled import DueEffect, ScheduleWhen
from buildbot_nix.service import scheduled_worktree_id
from buildbot_nix.webhooks import ChangeRequest, PrClosed

from .support import git, insert_build, insert_project

pytestmark = pytest.mark.usefixtures("fresh_work_queue")


def make_config(dsn: str, state_dir: Path, **kwargs: Any) -> Config:
    return Config(
        db_url=dsn,
        build_systems=["x86_64-linux"],
        domain="ci.test",
        url="http://ci.test",
        state_dir=state_dir,
        **kwargs,
    )


@pytest.fixture
def git_repo(upstream: Path) -> tuple[Path, str]:
    return upstream, git(upstream, "rev-parse", "HEAD")


# --- pure helpers ------------------------------------------------------


def test_scheduled_worktree_id_distinct_per_effect() -> None:
    when = ScheduleWhen()
    a = DueEffect(project_id=1, schedule_name="s", effect="deploy", when=when)
    b = DueEffect(project_id=1, schedule_name="s", effect="notify", when=when)
    assert scheduled_worktree_id(a, 1) != scheduled_worktree_id(b, 1)


def test_scheduled_worktree_id_sanitizes_traversal() -> None:
    due = DueEffect(
        project_id=1,
        schedule_name="../../../etc",
        effect="x/../../y",
        when=ScheduleWhen(),
    )
    wid = scheduled_worktree_id(due, 1)
    assert "/" not in wid
    assert ".." not in wid


def test_resolve_credential_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    assert resolve_credential_path(Path("name")) == Path("name")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/x")
    assert resolve_credential_path(Path("name")) == Path("/run/credentials/x/name")
    assert resolve_credential_path(Path("/abs/key")) == Path("/abs/key")
    assert resolve_credential_path(None) is None


# --- composition -------------------------------------------------------


def test_build_service_accepts_asyncpg_dsn(postgres_dsn: str, tmp_path: Path) -> None:
    """SQLAlchemy-style URLs must be normalized before apply_migrations
    too, not only for the pool."""

    async def run() -> None:
        dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://")
        service, _app = await build_service(make_config(dsn, tmp_path / "state"))
        try:
            assert await service.pool.fetchval("SELECT 1") == 1
        finally:
            await service.pool.close()

    asyncio.run(run())


def test_visibility_fetcher_and_cache_ttl_wired(
    postgres_dsn: str, tmp_path: Path
) -> None:
    async def run() -> None:
        config = make_config(postgres_dsn, tmp_path / "state", repo_acl_cache_ttl=123)
        service, app = await build_service(config)
        try:
            visibility = app.state.web_context.visibility
            assert visibility.fetcher is not None
            assert visibility.cache.ttl == 123
        finally:
            await service.pool.close()

    asyncio.run(run())


# --- restart semantics --------------------------------------------------


async def seed_project(pool: Any, url: str) -> int:
    return await insert_project(
        pool, forge_repo_id=f"svc-{time.monotonic_ns()}", url=url
    )


def test_pr_close_discards_queued_changes(postgres_dsn: str, tmp_path: Path) -> None:
    """A queued change event for a closed PR must not build it later."""

    async def run() -> None:
        service, _app = await build_service(
            make_config(postgres_dsn, tmp_path / "state")
        )
        pool = service.pool
        try:
            project_id = await seed_project(pool, "http://x")
            forge_repo_id = await pool.fetchval(
                "SELECT forge_repo_id FROM projects WHERE id = $1", project_id
            )
            await service.submit(
                ChangeRequest(
                    forge="github",
                    forge_repo_id=forge_repo_id,
                    branch="refs/pull/12/head",
                    commit_sha="abc",
                    pr_number=12,
                )
            )
            await service.submit(
                PrClosed(forge="github", forge_repo_id=forge_repo_id, pr_number=12)
            )
            handled: list[Any] = []

            async def fake_handle(event: Any, credentials: Any = None) -> None:
                handled.append((event, credentials))

            service.orchestrator.handle_change_event = fake_handle  # type: ignore[method-assign]
            await service.drain_work()
            assert handled == []
            status = await pool.fetchval(
                "SELECT status FROM work_queue WHERE kind = 'change'"
            )
            assert status == "done"
        finally:
            await pool.close()

    asyncio.run(run())


def test_restart_gitlab_mr_build_fetches_mr_refs(
    postgres_dsn: str, tmp_path: Path, git_repo: tuple[Path, str]
) -> None:
    """The re-eval restart path must fetch GitLab MR heads via
    refs/merge-requests/*, not refs/pull/*."""
    repo, _sha = git_repo

    async def run() -> None:
        service, _app = await build_service(
            make_config(postgres_dsn, tmp_path / "state")
        )
        pool = service.pool
        try:
            project_id = await insert_project(
                pool,
                forge="gitlab",
                forge_repo_id=f"svc-{time.monotonic_ns()}",
                url=f"file://{repo}",
            )
            # file:// forces the full transfer protocol; clone before
            # the MR ref exists so only the fetch refspec can bring it
            # in (see test_orchestrator.make_gitlab_mr_env).
            await service.orchestrator.repos.fetch(
                "gitlab/acme/widget", f"file://{repo}", ["+refs/heads/*:refs/heads/*"]
            )
            git(repo, "checkout", "-b", "mrsrc")
            (repo / "mr").write_text("x")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "mr")
            mr_sha = git(repo, "rev-parse", "HEAD")
            git(repo, "update-ref", "refs/merge-requests/9/head", mr_sha)
            git(repo, "checkout", "main")
            git(repo, "branch", "-D", "mrsrc")
            build_id = await insert_build(
                pool,
                project_id,
                commit_sha=mr_sha,
                status="failed",
                pr_number=9,
                error="eval boom",
            )

            reevals: list[int] = []

            async def fake_run_build(
                event: Any,
                build: Any,
                worktree_path: Path,
                credentials: Any = None,
            ) -> None:
                reevals.append(build.id)

            service.orchestrator.run_build = fake_run_build  # type: ignore[method-assign]
            await service.restart_build(build_id)
            await service.drain_work()
            await asyncio.gather(*service._tasks)  # noqa: SLF001
            assert reevals == [build_id]
        finally:
            # Module-shared database: an enabled gitlab project would
            # leak into the discovery/hook-registration tests.
            await pool.execute("DELETE FROM projects WHERE id = $1", project_id)
            await pool.close()

    asyncio.run(run())


def test_restart_eval_failed_build_reevaluates(
    postgres_dsn: str, tmp_path: Path, git_repo: tuple[Path, str]
) -> None:
    """A build that failed before eval produced attributes has nothing
    to resume; restarting it must re-evaluate instead of aggregating an
    empty attribute set to 'succeeded'."""
    repo, sha = git_repo

    async def run() -> None:
        service, _app = await build_service(
            make_config(postgres_dsn, tmp_path / "state")
        )
        pool = service.pool
        try:
            project_id = await seed_project(pool, str(repo))
            build_id = await insert_build(
                pool, project_id, commit_sha=sha, status="failed", error="eval boom"
            )

            reevals: list[int] = []

            async def fake_run_build(
                event: Any,
                build: Any,
                worktree_path: Path,
                credentials: Any = None,
            ) -> None:
                assert await asyncio.to_thread(worktree_path.exists)
                reevals.append(build.id)

            service.orchestrator.run_build = fake_run_build  # type: ignore[method-assign]
            await service.restart_build(build_id)
            await service.drain_work()
            # A restart only queues; the rerun sets the real status.
            status = await pool.fetchval(
                "SELECT status FROM builds WHERE id = $1", build_id
            )
            assert status == "pending"
            await asyncio.gather(*service._tasks)  # noqa: SLF001

            assert reevals == [build_id]
            status = await pool.fetchval(
                "SELECT status FROM builds WHERE id = $1", build_id
            )
            assert status != "succeeded"
        finally:
            await pool.close()

    asyncio.run(run())


def test_restart_failed_eval_attribute_reevaluates(
    postgres_dsn: str, tmp_path: Path, git_repo: tuple[Path, str]
) -> None:
    """failed_eval attributes have no drv_path; resetting them to
    pending must trigger a re-eval, not wedge the build in 'building'."""
    repo, sha = git_repo

    async def run() -> None:
        service, _app = await build_service(
            make_config(postgres_dsn, tmp_path / "state")
        )
        pool = service.pool
        try:
            project_id = await seed_project(pool, str(repo))
            build_id = await insert_build(
                pool, project_id, commit_sha=sha, status="failed"
            )
            await pool.execute(
                "INSERT INTO build_attributes (build_id, attr, system, status, "
                "error) VALUES ($1, 'broken', 'x86_64-linux', 'failed_eval', 'e')",
                build_id,
            )

            reevals: list[int] = []

            async def fake_run_build(
                event: Any,
                build: Any,
                worktree_path: Path,
                credentials: Any = None,
            ) -> None:
                reevals.append(build.id)

            service.orchestrator.run_build = fake_run_build  # type: ignore[method-assign]
            await service.restart_build(build_id)
            await service.drain_work()
            # A restart only queues; the rerun sets the real status.
            status = await pool.fetchval(
                "SELECT status FROM builds WHERE id = $1", build_id
            )
            assert status == "pending"
            await asyncio.gather(*service._tasks)  # noqa: SLF001

            assert reevals == [build_id]
            # Stale NULL-drv rows are cleared so the re-eval result is
            # authoritative.
            count = await pool.fetchval(
                "SELECT count(*) FROM build_attributes WHERE build_id = $1",
                build_id,
            )
            assert count == 0
        finally:
            await pool.close()

    asyncio.run(run())


# --- cancel of a non-running build ---------------------------------------


class RecordingReporter(NullStatusReporter):
    def __init__(self) -> None:
        self.finished: list[tuple[int, str, int]] = []

    async def build_finished(
        self,
        event: Any,
        build: Any,
        status: str,
        generation: int,
        results: list,
        **kwargs: Any,
    ) -> None:
        self.finished.append((build.id, status, generation))


def test_cancel_not_running_posts_forge_status(
    postgres_dsn: str, tmp_path: Path
) -> None:
    async def run() -> None:
        service, _app = await build_service(
            make_config(postgres_dsn, tmp_path / "state")
        )
        pool = service.pool
        try:
            project_id = await seed_project(pool, "http://example/repo")
            build_id = await insert_build(
                pool, project_id, commit_sha="c1", status="building"
            )
            reporter = RecordingReporter()
            service.orchestrator.reporter = reporter

            await service.cancel_build(build_id)

            assert (
                await pool.fetchval("SELECT status FROM builds WHERE id = $1", build_id)
                == "cancelled"
            )
            assert len(reporter.finished) == 1
            reported_id, status, generation = reporter.finished[0]
            assert (reported_id, status) == (build_id, "cancelled")
            assert generation == 1  # bumped so stale posts lose

            # Cancelling again is a no-op: no duplicate forge status.
            await service.cancel_build(build_id)
            assert len(reporter.finished) == 1
        finally:
            await pool.close()

    asyncio.run(run())


# --- pull-based projects --------------------------------------------------


def test_pull_based_projects_synced_and_buildable(
    postgres_dsn: str, tmp_path: Path
) -> None:
    """Polled head changes are dropped unless an enabled projects row
    exists for forge='pull_based'."""

    async def run() -> None:
        key = tmp_path / "id_ed25519"
        key.write_text("fake-key")
        config = make_config(
            postgres_dsn,
            tmp_path / "state",
            pull_based=PullBasedConfig(
                repositories={
                    "myrepo": PullBasedRepository(
                        name="myrepo",
                        default_branch="main",
                        url="ssh://git@example.com/x/y.git",
                        ssh_private_key_file=key,
                    )
                }
            ),
        )
        service, _app = await build_service(config)
        pool = service.pool
        try:
            await service.discover_once()
            row = await pool.fetchrow(
                "SELECT * FROM projects WHERE forge = 'pull_based' "
                "AND forge_repo_id = 'myrepo'"
            )
            assert row is not None
            assert row["enabled"] is True
            assert row["default_branch"] == "main"

            events: list[Any] = []

            async def fake_handle(event: Any, credentials: Any = None) -> None:
                events.append((event, credentials))

            service.orchestrator.handle_change_event = fake_handle  # type: ignore[method-assign]
            await service.submit(
                ChangeRequest(
                    forge="pull_based",
                    forge_repo_id="myrepo",
                    branch="main",
                    commit_sha="abc",
                )
            )
            await service.drain_work()
            assert len(events) == 1
            event, credentials = events[0]
            assert event.repo.forge == "pull_based"
            # SSH credentials resolved for the polled repository.
            assert credentials.ssh_private_key_file == key
        finally:
            await pool.close()

    asyncio.run(run())


# --- discovery topic handling ----------------------------------------------


class StubGitHub:
    def __init__(self, repos: list[DiscoveredRepo]) -> None:
        self.repos = repos

    async def discover_repos(self) -> list[DiscoveredRepo]:
        return self.repos


def test_topic_does_not_hard_filter_discovery(
    postgres_dsn: str, tmp_path: Path
) -> None:
    """The topic is a one-shot legacy import aid; repos without it must
    still be discovered (disabled) so admins can enable them in the UI."""

    async def run() -> None:
        secret = tmp_path / "gh.pem"
        secret.write_text("k")
        webhook_secret = tmp_path / "webhook-secret"
        webhook_secret.write_text("s")
        config = make_config(
            postgres_dsn,
            tmp_path / "state",
            github={
                "id": 1,
                "secret_key_file": str(secret),
                "webhook_secret_file": str(webhook_secret),
                "filters": {"topic": "build-with-buildbot"},
            },
        )
        service, _app = await build_service(config)
        pool = service.pool
        try:
            service.github = StubGitHub(  # type: ignore[assignment]
                [
                    DiscoveredRepo(
                        forge="github",
                        forge_repo_id="999001",
                        owner="acme",
                        repo="untagged",
                        default_branch="main",
                        clone_url="http://example/acme/untagged",
                        private=False,
                        topics=(),
                    )
                ]
            )
            await service.discover_once()
            row = await pool.fetchrow(
                "SELECT * FROM projects WHERE forge = 'github' "
                "AND forge_repo_id = '999001'"
            )
            assert row is not None
            assert row["name"] == "untagged"
        finally:
            await pool.close()

    asyncio.run(run())


def test_startup_reevaluates_interrupted_eval(
    postgres_dsn: str, tmp_path: Path, git_repo: tuple[Path, str]
) -> None:
    """A build interrupted mid-eval (no attribute rows) re-evaluates at
    startup instead of being marked failed."""
    repo, sha = git_repo

    async def run() -> None:
        service, _app = await build_service(
            make_config(postgres_dsn, tmp_path / "state")
        )
        pool = service.pool
        try:
            project_id = await seed_project(pool, str(repo))
            build_id = await insert_build(
                pool, project_id, commit_sha=sha, status="evaluating"
            )

            reevals: list[int] = []

            async def fake_run_build(
                event: Any,
                build: Any,
                worktree_path: Path,
                credentials: Any = None,
            ) -> None:
                reevals.append(build.id)

            service.orchestrator.run_build = fake_run_build  # type: ignore[method-assign]
            await _startup(service)
            await service.drain_work()
            await asyncio.gather(*service._tasks)  # noqa: SLF001

            # The shared database may hold other tests' unfinished builds.
            assert build_id in reevals
            status = await pool.fetchval(
                "SELECT status FROM builds WHERE id = $1", build_id
            )
            assert status != "failed"
        finally:
            await pool.close()

    asyncio.run(run())


def test_reeval_failure_marks_build_failed(
    postgres_dsn: str, tmp_path: Path, git_repo: tuple[Path, str]
) -> None:
    """A failed re-eval marks the build failed instead of leaving it
    pending."""
    repo, sha = git_repo

    async def run() -> None:
        service, _app = await build_service(
            make_config(postgres_dsn, tmp_path / "state")
        )
        pool = service.pool
        try:
            project_id = await seed_project(pool, str(repo))
            build_id = await insert_build(
                pool, project_id, commit_sha=sha, status="failed", error="boom"
            )

            async def broken_run_build(
                event: Any,
                build: Any,
                worktree_path: Path,
                credentials: Any = None,
            ) -> None:
                msg = "eval exploded"
                raise RuntimeError(msg)

            service.orchestrator.run_build = broken_run_build  # type: ignore[method-assign]
            await service.restart_build(build_id)
            await service.drain_work()
            await asyncio.gather(*service._tasks, return_exceptions=True)  # noqa: SLF001

            row = await pool.fetchrow(
                "SELECT status, error FROM builds WHERE id = $1", build_id
            )
            assert row["status"] == "failed"
            assert "re-evaluation" in row["error"]
        finally:
            await pool.close()

    asyncio.run(run())


def test_gitlab_discovery_and_hook_registration(
    postgres_dsn: str, tmp_path: Path
) -> None:
    hook_posts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v4/projects" and request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 41,
                        "path_with_namespace": "Mic92/dotfiles",
                        "default_branch": "main",
                        "http_url_to_repo": "https://gitlab.com/Mic92/dotfiles.git",
                        "visibility": "public",
                    }
                ],
            )
        if path.endswith("/hooks") and request.method == "GET":
            return httpx.Response(200, json=[])
        if path.endswith("/hooks") and request.method == "POST":
            hook_posts.append(json.loads(request.content))
            return httpx.Response(201, json={})
        return httpx.Response(404)

    async def run() -> None:
        token = tmp_path / "gitlab-token"
        token.write_text("glpat-x")
        config = make_config(
            postgres_dsn,
            tmp_path / "state",
            gitlab={"token_file": str(token)},
        )
        service, _app = await build_service(config)
        pool = service.pool
        try:
            service.gitlab = GitlabClient(
                "https://gitlab.com",
                "glpat-x",
                http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            )
            await service.discover_once()
            project = await pool.fetchrow(
                "SELECT * FROM projects WHERE forge = 'gitlab' AND forge_repo_id = '41'"
            )
            assert project is not None
            assert project["name"] == "dotfiles"
            assert not hook_posts  # disabled projects get no hook

            await pool.execute(
                "UPDATE projects SET enabled = TRUE WHERE id = $1", project["id"]
            )
            await service._register_hooks()  # noqa: SLF001
            assert hook_posts[0]["url"] == "http://ci.test/webhooks/gitlab"
            secret = await pool.fetchval(
                "SELECT secret FROM webhook_secrets WHERE project_id = $1",
                project["id"],
            )
            assert hook_posts[0]["token"] == secret
        finally:
            await pool.close()

    asyncio.run(run())


def test_health_serves_while_startup_blocks(
    postgres_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HTTP server binds while discovery/recovery still run."""

    async def run() -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        config = make_config(postgres_dsn, tmp_path / "state", http_port=port)

        started = asyncio.Event()

        async def stalled_startup(service: object) -> None:
            started.set()
            await asyncio.sleep(3600)

        monkeypatch.setattr("buildbot_nix.bootstrap._startup", stalled_startup)
        runner = asyncio.create_task(run_service(config))
        try:
            await asyncio.wait_for(started.wait(), timeout=30)
            async with httpx.AsyncClient() as client:
                for _ in range(100):
                    try:
                        response = await client.get(f"http://127.0.0.1:{port}/health")
                        break
                    except httpx.TransportError:
                        await asyncio.sleep(0.1)
                else:
                    msg = "server did not bind while startup was blocked"
                    raise AssertionError(msg)
            assert response.status_code == 200
        finally:
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner

    asyncio.run(run())
