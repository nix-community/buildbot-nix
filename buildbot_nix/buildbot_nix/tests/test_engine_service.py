"""Service composition regression tests (engine/service.py)."""

# ruff: noqa: PLR2004, ARG001, ARG002 (stub callbacks ignore arguments)

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from buildbot_nix.engine.config import (
    EngineConfig,
    PullBasedConfig,
    PullBasedRepository,
)
from buildbot_nix.engine.forge import DiscoveredRepo
from buildbot_nix.engine.migrations import apply_migrations
from buildbot_nix.engine.scheduled import DueEffect, ScheduleWhen
from buildbot_nix.engine.service import (
    build_service,
    resolve_credential_path,
    scheduled_worktree_id,
)
from buildbot_nix.engine.webhooks import ChangeRequest

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.skipif(
    shutil.which("initdb") is None, reason="postgresql not available"
)


@pytest.fixture(scope="module")
def postgres_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    datadir = tmp_path_factory.mktemp("pgdata")
    sockdir = tmp_path_factory.mktemp("pgsock")
    subprocess.run(  # noqa: S603
        ["initdb", "-D", str(datadir), "-U", "test", "--auth=trust"],
        check=True,
        capture_output=True,
    )
    proc = subprocess.Popen(  # noqa: S603
        ["postgres", "-D", str(datadir), "-k", str(sockdir), "-c", "listen_addresses="],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while not Path(sockdir, ".s.PGSQL.5432").exists():
            if time.monotonic() > deadline:
                msg = "postgres did not start"
                raise RuntimeError(msg)
            time.sleep(0.1)
        subprocess.run(  # noqa: S603
            ["createdb", "-h", str(sockdir), "-U", "test", "svc"],
            check=True,
            capture_output=True,
        )
        dsn = f"postgresql://test@/svc?host={sockdir}"
        asyncio.run(apply_migrations(dsn))
        yield dsn
    finally:
        # Immediate shutdown: SIGTERM is postgres "smart" shutdown and
        # would wait forever for any lingering test connection.
        proc.send_signal(signal.SIGQUIT)
        proc.wait()


def make_config(dsn: str, state_dir: Path, **kwargs: Any) -> EngineConfig:
    return EngineConfig(
        db_url=dsn,
        build_systems=["x86_64-linux"],
        domain="ci.test",
        url="http://ci.test",
        state_dir=state_dir,
        **kwargs,
    )


def git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "origin"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "flake.nix").write_text("{}")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    return repo, git(repo, "rev-parse", "HEAD")


# --- pure helpers ------------------------------------------------------


def test_scheduled_worktree_id_distinct_per_effect() -> None:
    when = ScheduleWhen()
    a = DueEffect(project_id=1, schedule_name="s", effect="deploy", when=when)
    b = DueEffect(project_id=1, schedule_name="s", effect="notify", when=when)
    assert scheduled_worktree_id(a) != scheduled_worktree_id(b)


def test_scheduled_worktree_id_sanitizes_traversal() -> None:
    due = DueEffect(
        project_id=1,
        schedule_name="../../../etc",
        effect="x/../../y",
        when=ScheduleWhen(),
    )
    wid = scheduled_worktree_id(due)
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
    return await pool.fetchval(
        "INSERT INTO projects (forge, forge_repo_id, owner, name, url, "
        "default_branch, enabled) VALUES "
        "('github', $1, 'acme', 'widget', $2, 'main', TRUE) RETURNING id",
        f"svc-{time.monotonic_ns()}",
        url,
    )


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
            build_id = await pool.fetchval(
                "INSERT INTO builds (project_id, number, branch, commit_sha, "
                "status, error) VALUES ($1, 1, 'main', $2, 'failed', 'eval boom') "
                "RETURNING id",
                project_id,
                sha,
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
            build_id = await pool.fetchval(
                "INSERT INTO builds (project_id, number, branch, commit_sha, "
                "status) VALUES ($1, 1, 'main', $2, 'failed') RETURNING id",
                project_id,
                sha,
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


@dataclass
class RecordingReporter:
    finished: list[tuple[int, str, int]] = field(default_factory=list)

    async def build_started(self, event: Any, build: Any) -> None:
        pass

    async def eval_finished(
        self, event: Any, build: Any, *, success: bool, warnings: list[str]
    ) -> None:
        pass

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
            build_id = await pool.fetchval(
                "INSERT INTO builds (project_id, number, branch, commit_sha, "
                "status) VALUES ($1, 1, 'main', 'c1', 'building') RETURNING id",
                project_id,
            )
            reporter = RecordingReporter()
            service.orchestrator.reporter = reporter  # type: ignore[assignment]

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
            await service._submit_change(  # noqa: SLF001
                ChangeRequest(
                    forge="pull_based",
                    forge_repo_id="myrepo",
                    branch="main",
                    commit_sha="abc",
                )
            )
            await asyncio.gather(*service._tasks)  # noqa: SLF001
            assert len(events) == 1
            event, credentials = events[0]
            assert event.project.forge == "pull_based"
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
