"""Orchestrator/state-machine tests: ephemeral Postgres,
real git repos, fake eval and executor."""

# ruff: noqa: PLR2004, ARG001, ARG002 (test literals; protocol fakes ignore args)

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import asyncpg
import pytest

from buildbot_nix import orchestrator as orch_mod
from buildbot_nix.config import Config
from buildbot_nix.db import BuildDB, BuildStatus
from buildbot_nix.events import ChangeEvent, RepoInfo
from buildbot_nix.gitrepo import FetchCredentials, RepoManager
from buildbot_nix.memory import calculate_eval_workers
from buildbot_nix.nix_eval import EvalError, EvalResult
from buildbot_nix.orchestrator import Orchestrator
from buildbot_nix.scheduler import (
    AttributeResult,
    AttributeStatus,
    BuildOutcome,
)

from .support import ephemeral_postgres, mk_job

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from buildbot_nix.db import BuildRecord
    from buildbot_nix.models import NixEvalJobSuccess

pytestmark = pytest.mark.skipif(
    shutil.which("initdb") is None or shutil.which("git") is None,
    reason="postgresql or git not available",
)


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def postgres_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    with ephemeral_postgres(tmp_path_factory, "orch") as dsn:
        yield dsn


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
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    ).stdout.strip()


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    repo = tmp_path / "upstream"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "flake.nix").write_text("{}")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    return repo


# --- fakes --------------------------------------------------------------------


@dataclass
class FakeEvalRunner:
    jobs: list[NixEvalJobSuccess]
    warnings: list[str] = field(default_factory=list)
    calls: int = 0
    last_settings: object = None
    block: asyncio.Event | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self, *args: object, **kwargs: object) -> EvalResult:
        self.calls += 1
        self.started.set()
        if len(args) > 2:
            self.last_settings = args[2]
            os.makedirs(args[2].gc_roots_dir, exist_ok=True)  # type: ignore[attr-defined]  # noqa: PTH103
        if self.block is not None:
            await self.block.wait()
        return EvalResult(jobs=list(self.jobs), warnings=list(self.warnings))


@dataclass
class FakeExecutor:
    outcomes: dict[str, BuildOutcome] = field(default_factory=dict)
    built: list[str] = field(default_factory=list)
    # When set, builds wait on this gate (for in-flight tests).
    gate: asyncio.Event | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def build_attribute(
        self,
        build_key: object,
        job: NixEvalJobSuccess,
        log_writer: object,
        cwd: object,
        cancel_event: object = None,
    ) -> BuildOutcome:
        self.built.append(job.attr)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        await log_writer.write(b"fake build output\n")  # type: ignore[attr-defined]
        return self.outcomes.get(job.attr, BuildOutcome.success)


@dataclass
class RecordingReporter:
    events: list[tuple] = field(default_factory=list)

    async def build_started(self, event: ChangeEvent, build: BuildRecord) -> None:
        self.events.append(("started", build.id))

    async def eval_finished(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        *,
        success: bool,
        warnings: list[str],
    ) -> None:
        self.events.append(("eval", build.id, success, tuple(warnings)))

    async def eval_cancelled(self, event: ChangeEvent, build: BuildRecord) -> None:
        self.events.append(("eval-cancelled", build.id))

    async def build_finished(  # noqa: PLR0913
        self,
        event: ChangeEvent,
        build: BuildRecord,
        status: str,
        generation: int,
        results: list[AttributeResult],
        *,
        attr_statuses: dict[str, str] | None = None,
        attr_prefix: str = "checks",
    ) -> None:
        self.events.append(("finished", build.id, status, generation))


# --- helpers --------------------------------------------------------------------


async def make_project(pool: asyncpg.Pool, name: str = "widget") -> RepoInfo:
    project_id = await pool.fetchval(
        """
        INSERT INTO projects (forge, forge_repo_id, owner, name, default_branch,
                              url, enabled)
        VALUES ('github', $2, 'acme', $1, 'main', 'https://x', TRUE)
        ON CONFLICT (forge, forge_repo_id) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """,
        name,
        f"id-{name}",
    )
    return RepoInfo(
        id=project_id,
        key=f"github/acme/{name}",
        name=f"acme/{name}",
        owner="acme",
        repo=name,
        forge="github",
        clone_url="",  # set per test
        default_branch="main",
    )


def make_orchestrator(
    dsn_pool: asyncpg.Pool,
    tmp_path: Path,
    eval_runner: FakeEvalRunner,
    executor: FakeExecutor,
) -> tuple[Orchestrator, RecordingReporter]:
    config = Config(
        db_url="unused",
        build_systems=["x86_64-linux"],
        domain="ci.test",
        url="https://ci.test",
        state_dir=tmp_path / "state",
    )
    reporter = RecordingReporter()
    registered: list[tuple[Path, str, str, str]] = []

    async def fake_register(
        gcroots_dir: Path, project: str, attr: str, out: str
    ) -> None:
        registered.append((gcroots_dir, project, attr, out))

    orchestrator = Orchestrator(
        config=config,
        db=BuildDB(dsn_pool),
        repos=RepoManager(config.state_dir),
        eval_runner=eval_runner,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        reporter=reporter,
        register_gcroot=fake_register,
    )
    return orchestrator, reporter


async def run_event(
    dsn: str,
    tmp_path: Path,
    upstream: Path,
    eval_runner: FakeEvalRunner,
    executor: FakeExecutor,
    **event_kwargs: object,
) -> tuple[BuildRecord | None, RecordingReporter, asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn)
    orchestrator, reporter = make_orchestrator(pool, tmp_path, eval_runner, executor)
    project = await make_project(pool)
    project = RepoInfo(**{**project.__dict__, "clone_url": str(upstream)})
    event = ChangeEvent(
        repo=project,
        branch="main",
        commit_sha=git(upstream, "rev-parse", "HEAD"),
        **event_kwargs,  # type: ignore[arg-type]
    )
    build = await orchestrator.handle_change_event(event)
    return build, reporter, pool


def add_commit(upstream: Path, name: str) -> str:
    (upstream / name).write_text("x")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", name)
    return git(upstream, "rev-parse", "HEAD")


async def make_env(  # noqa: PLR0913
    dsn: str,
    tmp_path: Path,
    upstream: Path,
    eval_runner: object,
    executor: object,
    name: str,
) -> tuple[asyncpg.Pool, Orchestrator, RecordingReporter, RepoInfo]:
    pool = await asyncpg.create_pool(dsn)
    orchestrator, reporter = make_orchestrator(
        pool,
        tmp_path,
        eval_runner,  # type: ignore[arg-type]
        executor,  # type: ignore[arg-type]
    )
    project = await make_project(pool, name=name)
    project = RepoInfo(**{**project.__dict__, "clone_url": str(upstream)})
    return pool, orchestrator, reporter, project


async def build_status(pool: asyncpg.Pool, build_id: int) -> str:
    return await pool.fetchval("SELECT status FROM builds WHERE id = $1", build_id)


# --- tests --------------------------------------------------------------------


def test_full_lifecycle(postgres_dsn: str, tmp_path: Path, upstream: Path) -> None:
    async def run() -> None:
        eval_runner = FakeEvalRunner([mk_job("a"), mk_job("b")], warnings=["warn1"])
        executor = FakeExecutor()
        build, reporter, pool = await run_event(
            postgres_dsn, tmp_path, upstream, eval_runner, executor
        )
        try:
            assert build is not None
            row = await pool.fetchrow("SELECT * FROM builds WHERE id = $1", build.id)
            assert row["status"] == BuildStatus.SUCCEEDED
            assert row["status_generation"] == 1
            assert row["tree_hash"]
            attrs = await pool.fetch(
                "SELECT attr, status FROM build_attributes WHERE build_id = $1",
                build.id,
            )
            assert {r["attr"]: r["status"] for r in attrs} == {
                "a": "succeeded",
                "b": "succeeded",
            }
            # Log metadata written in the same transaction as completion.
            logs = await pool.fetch(
                "SELECT l.path FROM logs l JOIN build_attributes a "
                "ON l.attribute_id = a.id WHERE a.build_id = $1",
                build.id,
            )
            assert len(logs) == 2
            kinds = [e[0] for e in reporter.events]
            assert kinds == ["started", "eval", "finished"]
            assert reporter.events[1][3] == ("warn1",)
            assert reporter.events[2][2] == BuildStatus.SUCCEEDED
        finally:
            await pool.close()

    asyncio.run(run())


def test_failure_aggregation(postgres_dsn: str, tmp_path: Path, upstream: Path) -> None:
    async def run() -> None:
        add_commit(upstream, "f2")
        eval_runner = FakeEvalRunner([mk_job("ok"), mk_job("bad")])
        executor = FakeExecutor({"bad": BuildOutcome.failure})
        build, reporter, pool = await run_event(
            postgres_dsn, tmp_path, upstream, eval_runner, executor
        )
        try:
            assert build is not None
            assert await build_status(pool, build.id) == BuildStatus.FAILED
            assert reporter.events[-1][2] == BuildStatus.FAILED
            error = await pool.fetchval(
                "SELECT error FROM build_attributes"
                " WHERE build_id = $1 AND attr = 'bad'",
                build.id,
            )
            assert error is not None
            assert "fake build output" in error
        finally:
            await pool.close()

    asyncio.run(run())


def test_tree_hash_reuse(postgres_dsn: str, tmp_path: Path, upstream: Path) -> None:
    async def run() -> None:
        add_commit(upstream, "f3")
        eval_runner = FakeEvalRunner([mk_job("a")])
        executor = FakeExecutor()
        build1, _, pool1 = await run_event(
            postgres_dsn, tmp_path, upstream, eval_runner, executor
        )
        await pool1.close()
        # Same tree content from another context: no second build, no
        # second eval; existing result re-reported.
        build2, reporter2, pool2 = await run_event(
            postgres_dsn, tmp_path, upstream, eval_runner, executor
        )
        try:
            assert build1 is not None
            assert build2 is not None
            assert build1.id == build2.id
            assert eval_runner.calls == 1
            # The eval context must not stay pending on the second commit.
            assert [e[0] for e in reporter2.events] == ["eval", "finished"]
        finally:
            await pool2.close()

    asyncio.run(run())


def test_merge_conflict_fails_build(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    async def run() -> None:
        git(upstream, "checkout", "-b", "pr")
        (upstream / "flake.nix").write_text("{ pr = 1; }")
        git(upstream, "commit", "-am", "pr change")
        head = git(upstream, "rev-parse", "HEAD")
        git(upstream, "checkout", "main")
        (upstream / "flake.nix").write_text("{ main = 1; }")
        git(upstream, "commit", "-am", "main change")
        base = git(upstream, "rev-parse", "HEAD")

        eval_runner = FakeEvalRunner([mk_job("a")])
        executor = FakeExecutor()
        pool = await asyncpg.create_pool(postgres_dsn)
        orchestrator, reporter = make_orchestrator(
            pool, tmp_path, eval_runner, executor
        )
        project = await make_project(pool, name="conflict")
        project = RepoInfo(**{**project.__dict__, "clone_url": str(upstream)})
        try:
            build = await orchestrator.handle_change_event(
                ChangeEvent(
                    repo=project,
                    branch="main",
                    commit_sha=head,
                    pr_number=5,
                    pr_author="github:alice",
                    base_sha=base,
                )
            )
            assert build is not None
            row = await pool.fetchrow("SELECT * FROM builds WHERE id = $1", build.id)
            assert row["status"] == "failed"
            assert "conflict" in row["error"]
            assert row["pr_author"] == "github:alice"
            assert eval_runner.calls == 0
            assert reporter.events[-1][2] == BuildStatus.FAILED
            # The eval context must get a terminal status too.
            assert ("eval", build.id, False, ()) in reporter.events
        finally:
            await pool.close()

    asyncio.run(run())


def test_build_reuse_clears_pr_author_in_other_context(
    postgres_dsn: str, tmp_path: Path
) -> None:
    """A PR build reused for another context (e.g. the default branch
    after the merge) must not stay controllable by the PR author."""

    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            db = BuildDB(pool)
            project = await make_project(pool, name="reuse-author")
            build, created = await db.get_or_create_build(
                project.id,
                "tree-reuse",
                "sha",
                "main",
                pr_number=7,
                pr_author="github:alice",
            )
            assert created

            # Re-trigger of the same PR: author keeps control.
            await db.get_or_create_build(
                project.id, "tree-reuse", "sha", "main", pr_number=7
            )
            author = await pool.fetchval(
                "SELECT pr_author FROM builds WHERE id = $1", build.id
            )
            assert author == "github:alice"

            # Default-branch push producing the same tree: control revoked.
            _, created = await db.get_or_create_build(
                project.id, "tree-reuse", "sha", "main"
            )
            assert not created
            author = await pool.fetchval(
                "SELECT pr_author FROM builds WHERE id = $1", build.id
            )
            assert author is None
        finally:
            await pool.close()

    asyncio.run(run())


def test_effects_started_flag(postgres_dsn: str, tmp_path: Path) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            db = BuildDB(pool)
            project = await make_project(pool, name="flag")
            build, _ = await db.get_or_create_build(
                project.id, "tree-flag", "sha", "main"
            )
            assert await db.mark_effects_started(build.id)
            # Second attempt (e.g. crash recovery) must not re-run.
            assert not await db.mark_effects_started(build.id)
        finally:
            await pool.close()

    asyncio.run(run())


def test_aggregation_generation_monotonic(postgres_dsn: str, tmp_path: Path) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            db = BuildDB(pool)
            project = await make_project(pool, name="gen")
            build, _ = await db.get_or_create_build(
                project.id, "tree-gen", "sha", "main"
            )
            job = mk_job("x")
            await db.complete_attribute(
                build.id,
                AttributeResult(
                    attr="x",
                    status=AttributeStatus.failed,
                    job=job,
                    drv_path=job.drv_path,
                    system=job.system,
                ),
            )
            status1, gen1 = await db.aggregate_build(build.id)
            assert status1 == BuildStatus.FAILED
            # Attribute rebuilt successfully: aggregate flips, generation grows.
            await db.complete_attribute(
                build.id,
                AttributeResult(
                    attr="x",
                    status=AttributeStatus.succeeded,
                    job=job,
                    drv_path=job.drv_path,
                    system=job.system,
                ),
            )
            status2, gen2 = await db.aggregate_build(build.id)
            assert status2 == BuildStatus.SUCCEEDED
            assert gen2 > gen1
        finally:
            await pool.close()

    asyncio.run(run())


def test_internal_error_not_recorded_in_failed_build_cache(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    async def run() -> None:
        class CrashingExecutor(FakeExecutor):
            async def build_attribute(
                self, *args: object, **kwargs: object
            ) -> BuildOutcome:
                msg = "executor exploded"
                raise RuntimeError(msg)

        class RecordingCache:
            def __init__(self) -> None:
                self.added: list[str] = []

            async def check(self, drv_path: str) -> None:
                return None

            async def add(self, drv_path: str, url: str) -> None:
                self.added.append(drv_path)

            async def remove(self, drv_path: str) -> None:
                pass

        sha = add_commit(upstream, "crash")
        pool, orchestrator, _, project = await make_env(
            postgres_dsn,
            tmp_path,
            upstream,
            FakeEvalRunner([mk_job("a")]),
            CrashingExecutor(),
            "crash",
        )
        cache = RecordingCache()
        orchestrator.failed_build_cache = cache  # type: ignore[assignment]
        orchestrator.config = orchestrator.config.model_copy(
            update={"cache_failed_builds": True}
        )
        try:
            build = await orchestrator.handle_change_event(
                ChangeEvent(repo=project, branch="main", commit_sha=sha)
            )
            assert build is not None
            assert await build_status(pool, build.id) == BuildStatus.FAILED
            assert cache.added == []
        finally:
            await pool.close()

    asyncio.run(run())


def test_log_path_attribute_name_sanitized(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    async def run() -> None:
        add_commit(upstream, "trav")
        evil = '../../../evil".attr'
        build, _, pool = await run_event(
            postgres_dsn,
            tmp_path,
            upstream,
            FakeEvalRunner([mk_job(evil)]),
            FakeExecutor(),
        )
        try:
            assert build is not None
            log_dir = tmp_path / "state" / "logs" / str(build.id)
            files = list(log_dir.iterdir())
            assert len(files) == 1
            assert files[0].parent == log_dir
            assert not (tmp_path / 'evil".attr.zst').exists()
            log_path = await pool.fetchval(
                "SELECT l.path FROM logs l JOIN build_attributes a "
                "ON l.attribute_id = a.id WHERE a.build_id = $1",
                build.id,
            )
            state_dir = (tmp_path / "state").resolve()
            assert (state_dir / log_path).resolve().is_relative_to(state_dir / "logs")
        finally:
            await pool.close()

    asyncio.run(run())


def test_cancel_interrupts_evaluation(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    async def run() -> None:
        sha = add_commit(upstream, "ceval")
        eval_runner = FakeEvalRunner([mk_job("a")], block=asyncio.Event())
        executor = FakeExecutor()
        pool, orchestrator, reporter, project = await make_env(
            postgres_dsn, tmp_path, upstream, eval_runner, executor, "ceval"
        )
        try:
            task = asyncio.create_task(
                orchestrator.handle_change_event(
                    ChangeEvent(repo=project, branch="main", commit_sha=sha)
                )
            )
            await asyncio.wait_for(eval_runner.started.wait(), timeout=10)
            for cancel_event in orchestrator.cancel_events.values():
                cancel_event.set()
            build = await asyncio.wait_for(task, timeout=10)
            assert build is not None
            assert await build_status(pool, build.id) == BuildStatus.CANCELLED
            assert executor.built == []
            # The pending nix-eval status must resolve (merge queues).
            assert ("eval-cancelled", build.id) in reporter.events
        finally:
            await pool.close()

    asyncio.run(run())


def test_pending_attribute_rows_recorded_for_crash_recovery(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    """Without pending rows a crash mid-build has nothing to resume from."""

    async def run() -> None:
        sha = add_commit(upstream, "recov")
        executor = FakeExecutor(gate=asyncio.Event())
        pool, orchestrator, _, project = await make_env(
            postgres_dsn,
            tmp_path,
            upstream,
            FakeEvalRunner([mk_job("a"), mk_job("b")]),
            executor,
            "recov",
        )
        try:
            task = asyncio.create_task(
                orchestrator.handle_change_event(
                    ChangeEvent(repo=project, branch="main", commit_sha=sha)
                )
            )
            await asyncio.wait_for(executor.started.wait(), timeout=10)
            rows = await pool.fetch(
                "SELECT a.attr, a.status FROM build_attributes a "
                "JOIN builds b ON a.build_id = b.id WHERE b.project_id = $1",
                project.id,
            )
            assert {r["attr"] for r in rows} == {"a", "b"}
            # Both attrs may already be 'building' depending on timing;
            # recovery only needs the unfinished rows to exist.
            assert {r["status"] for r in rows} <= {"pending", "building"}
            assert executor.gate is not None
            executor.gate.set()
            build = await task
            assert build is not None
            assert await build_status(pool, build.id) == BuildStatus.SUCCEEDED
        finally:
            await pool.close()

    asyncio.run(run())


def test_eval_warnings_null_when_empty(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    async def run() -> None:
        add_commit(upstream, "warn")
        build, _, pool = await run_event(
            postgres_dsn,
            tmp_path,
            upstream,
            FakeEvalRunner([mk_job("a")]),
            FakeExecutor(),
        )
        try:
            assert build is not None
            warnings = await pool.fetchval(
                "SELECT eval_warnings FROM builds WHERE id = $1", build.id
            )
            assert warnings is None
        finally:
            await pool.close()

    asyncio.run(run())


def test_rerun_fetches_pr_refs(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    """PR heads are only reachable via refs/pull/*."""

    async def run() -> None:
        git(upstream, "checkout", "-b", "prsrc")
        pr_sha = add_commit(upstream, "pr")
        git(upstream, "update-ref", "refs/pull/7/head", pr_sha)
        git(upstream, "checkout", "main")
        git(upstream, "branch", "-D", "prsrc")

        pool, orchestrator, _, project = await make_env(
            postgres_dsn,
            tmp_path,
            upstream,
            FakeEvalRunner([]),
            FakeExecutor(),
            "prref",
        )
        db = BuildDB(pool)
        build, _ = await db.get_or_create_build(
            project.id, "pr-tree", pr_sha, "main", pr_number=7
        )
        try:
            await orchestrator.rerun_pending_attributes(project, build, [mk_job("a")])
            assert await db.get_attribute_statuses(build.id) == {"a": "succeeded"}
        finally:
            await pool.close()

    asyncio.run(run())


def test_cancelled_build_reuse_rebuilds(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    async def run() -> None:
        add_commit(upstream, "cxl")
        eval_runner = FakeEvalRunner([mk_job("a")])
        build1, _, pool1 = await run_event(
            postgres_dsn, tmp_path, upstream, eval_runner, FakeExecutor()
        )
        assert build1 is not None
        await pool1.execute(
            "UPDATE builds SET status = 'cancelled' WHERE id = $1", build1.id
        )
        await pool1.close()
        build2, _, pool2 = await run_event(
            postgres_dsn, tmp_path, upstream, eval_runner, FakeExecutor()
        )
        try:
            # A cancelled build carries no verdict: a re-push of the
            # same tree gets a fresh build instead of reusing it.
            assert build2 is not None
            assert build2.id != build1.id
            assert eval_runner.calls == 2
            assert await build_status(pool2, build2.id) == BuildStatus.SUCCEEDED
        finally:
            await pool2.close()

    asyncio.run(run())


def test_inflight_share_fans_out_final_status(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    async def run() -> None:
        sha = add_commit(upstream, "share")
        git(upstream, "branch", "copy", "main")
        executor = FakeExecutor(gate=asyncio.Event())
        pool, orchestrator, reporter, project = await make_env(
            postgres_dsn,
            tmp_path,
            upstream,
            FakeEvalRunner([mk_job("a")]),
            executor,
            "share",
        )
        try:
            task = asyncio.create_task(
                orchestrator.handle_change_event(
                    ChangeEvent(repo=project, branch="main", commit_sha=sha)
                )
            )
            await asyncio.wait_for(executor.started.wait(), timeout=10)
            build2 = await orchestrator.handle_change_event(
                ChangeEvent(repo=project, branch="copy", commit_sha=sha)
            )
            assert executor.gate is not None
            executor.gate.set()
            build1 = await task
            assert build1 is not None
            assert build2 is not None
            assert build1.id == build2.id
            finished = [e for e in reporter.events if e[0] == "finished"]
            assert len(finished) == 2  # one final status per context
        finally:
            await pool.close()

    asyncio.run(run())


def test_stale_event_does_not_supersede_newer_build(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    async def run() -> None:
        old_sha = add_commit(upstream, "older")
        new_sha = add_commit(upstream, "newer")
        executor = FakeExecutor(gate=asyncio.Event())
        pool, orchestrator, _, project = await make_env(
            postgres_dsn,
            tmp_path,
            upstream,
            FakeEvalRunner([mk_job("a")]),
            executor,
            "stale",
        )
        try:
            task = asyncio.create_task(
                orchestrator.handle_change_event(
                    ChangeEvent(repo=project, branch="main", commit_sha=new_sha)
                )
            )
            await asyncio.wait_for(executor.started.wait(), timeout=10)
            stale_build = await orchestrator.handle_change_event(
                ChangeEvent(repo=project, branch="main", commit_sha=old_sha)
            )
            assert stale_build is not None
            assert await build_status(pool, stale_build.id) == BuildStatus.CANCELLED
            assert executor.gate is not None
            executor.gate.set()
            build = await task
            assert build is not None
            assert await build_status(pool, build.id) == BuildStatus.SUCCEEDED
        finally:
            await pool.close()

    asyncio.run(run())


def test_eval_failure_cleans_cancel_events(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    """A leaked cancel_events entry blocks restart/cancel forever."""

    async def run() -> None:
        sha = add_commit(upstream, "evf")

        class FailingEval:
            async def run(self, *args: object, **kwargs: object) -> EvalResult:
                msg = "boom"
                raise EvalError(msg)

        pool, orchestrator, _, project = await make_env(
            postgres_dsn, tmp_path, upstream, FailingEval(), FakeExecutor(), "evf"
        )
        try:
            build = await orchestrator.handle_change_event(
                ChangeEvent(repo=project, branch="main", commit_sha=sha)
            )
            assert build is not None
            assert orchestrator.cancel_events == {}
        finally:
            await pool.close()

    asyncio.run(run())


def test_eval_settings_wired(postgres_dsn: str, tmp_path: Path, upstream: Path) -> None:
    async def run() -> None:
        sha = add_commit(upstream, "setw")
        eval_runner = FakeEvalRunner([mk_job("a")])
        pool, orchestrator, _, project = await make_env(
            postgres_dsn, tmp_path, upstream, eval_runner, FakeExecutor(), "setw"
        )
        netrc = tmp_path / "netrc"
        netrc.write_text("")
        try:
            await orchestrator.handle_change_event(
                ChangeEvent(repo=project, branch="main", commit_sha=sha),
                FetchCredentials(netrc_file=netrc),
            )
            settings = eval_runner.last_settings
            assert settings is not None
            assert settings.worker_count == calculate_eval_workers().count  # type: ignore[attr-defined]
            assert settings.netrc_file == netrc  # type: ignore[attr-defined]
        finally:
            await pool.close()

    asyncio.run(run())


def test_eval_gcroots_dir_removed_after_build(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    async def run() -> None:
        add_commit(upstream, "gcr")
        build, _, pool = await run_event(
            postgres_dsn,
            tmp_path,
            upstream,
            FakeEvalRunner([mk_job("a")]),
            FakeExecutor(),
        )
        try:
            assert build is not None
            assert not (tmp_path / "state" / "eval-gcroots" / str(build.id)).exists()
        finally:
            await pool.close()

    asyncio.run(run())


def test_effects_run_after_default_branch_success(
    postgres_dsn: str, tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        ran: list[str] = []

        async def fake_list(ctx: object) -> list[str]:
            return ["deploy"]

        async def fake_run(ctx: object, name: str, log_write: object = None) -> bool:
            ran.append(name)
            return True

        monkeypatch.setattr(orch_mod, "list_effects", fake_list)
        monkeypatch.setattr(orch_mod, "run_effect", fake_run)

        add_commit(upstream, "eff")
        build, _, pool = await run_event(
            postgres_dsn,
            tmp_path,
            upstream,
            FakeEvalRunner([mk_job("a")]),
            FakeExecutor(),
        )
        try:
            assert build is not None
            assert ran == ["deploy"]
            started = await pool.fetchval(
                "SELECT effects_started FROM builds WHERE id = $1", build.id
            )
            assert started is True
        finally:
            await pool.close()

    asyncio.run(run())


def test_unsupported_system_attr_does_not_block_aggregation(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    """The scheduler drops unsupported systems; a pending row for them
    would keep the build non-terminal forever."""

    async def run() -> None:
        add_commit(upstream, "sys")
        jobs = [mk_job("a"), mk_job("other", system="riscv64-linux")]
        build, _, pool = await run_event(
            postgres_dsn, tmp_path, upstream, FakeEvalRunner(jobs), FakeExecutor()
        )
        try:
            assert build is not None
            assert await build_status(pool, build.id) == BuildStatus.SUCCEEDED
            attrs = await pool.fetch(
                "SELECT attr, status FROM build_attributes WHERE build_id = $1",
                build.id,
            )
            assert {r["attr"]: r["status"] for r in attrs} == {"a": "succeeded"}
        finally:
            await pool.close()

    asyncio.run(run())


def test_linked_context_reported_on_eval_failure(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    async def run() -> None:
        sha = add_commit(upstream, "lef")
        git(upstream, "branch", "lef-copy", "main")

        class BlockedFailingEval:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.block = asyncio.Event()

            async def run(self, *args: object, **kwargs: object) -> EvalResult:
                self.started.set()
                await self.block.wait()
                msg = "boom"
                raise EvalError(msg)

        eval_runner = BlockedFailingEval()
        pool, orchestrator, reporter, project = await make_env(
            postgres_dsn, tmp_path, upstream, eval_runner, FakeExecutor(), "lef"
        )
        try:
            task = asyncio.create_task(
                orchestrator.handle_change_event(
                    ChangeEvent(repo=project, branch="main", commit_sha=sha)
                )
            )
            await asyncio.wait_for(eval_runner.started.wait(), timeout=10)
            build2 = await orchestrator.handle_change_event(
                ChangeEvent(repo=project, branch="lef-copy", commit_sha=sha)
            )
            eval_runner.block.set()
            build1 = await task
            assert build1 is not None
            assert build2 is not None
            assert build1.id == build2.id
            finished = [e for e in reporter.events if e[0] == "finished"]
            assert len(finished) == 2
            assert all(e[2] == BuildStatus.FAILED for e in finished)
        finally:
            await pool.close()

    asyncio.run(run())


def test_eval_failure_settles_streamed_attributes(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    # Eval streams a batch (rows recorded as pending), then fails:
    # the rows must not stay pending/building on the failed build.
    def _test() -> None:
        async def run() -> None:
            sha = add_commit(upstream, "evstream")

            class StreamingFailingEval:
                async def run(
                    self, *args: object, on_jobs: object = None, **kwargs: object
                ) -> EvalResult:
                    assert callable(on_jobs)
                    await on_jobs([mk_job("streamed")])
                    msg = "boom after streaming"
                    raise EvalError(msg)

            pool, orchestrator, _, project = await make_env(
                postgres_dsn,
                tmp_path,
                upstream,
                StreamingFailingEval(),
                FakeExecutor(),
                "evstream",
            )
            try:
                build = await orchestrator.handle_change_event(
                    ChangeEvent(repo=project, branch="main", commit_sha=sha)
                )
                assert build is not None
                assert await build_status(pool, build.id) == BuildStatus.FAILED
                rows = await pool.fetch(
                    "SELECT attr, status FROM build_attributes WHERE build_id = $1",
                    build.id,
                )
                assert {r["attr"]: r["status"] for r in rows} == {
                    "streamed": "cancelled"
                }
            finally:
                await pool.close()

        asyncio.run(run())

    _test()


def test_effect_rows_roundtrip(postgres_dsn: str, tmp_path: Path) -> None:
    """Start, finish and rerun-reset of effect rows."""

    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            db = BuildDB(pool)
            project = await make_project(pool, name="fx")
            build, _ = await db.get_or_create_build(
                project.id, "tree-fx", "sha", "main"
            )

            await db.start_effect(build.id, "deploy")
            await db.finish_effect(
                build.id,
                "deploy",
                success=False,
                error="ssh: connection refused",
                log_path="logs/1/effect-deploy.zst",
                log_size=123,
            )
            effects = await db.effects_for_build(build.id)
            assert [(e["name"], e["status"], e["error"]) for e in effects] == [
                ("deploy", "failed", "ssh: connection refused")
            ]
            assert effects[0]["finished_at"] is not None

            # A rerun resets the row to running with fresh timestamps.
            await db.start_effect(build.id, "deploy")
            effects = await db.effects_for_build(build.id)
            assert effects[0]["status"] == "running"
            assert effects[0]["finished_at"] is None
            assert effects[0]["error"] is None
        finally:
            await pool.close()

    asyncio.run(run())


def test_effect_crash_settles_the_row(
    postgres_dsn: str, tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected exception inside an effect run must not leave the
    row running: nothing re-runs effects, so it would stick until the
    next service restart."""

    async def run() -> None:
        async def fake_list(ctx: object) -> list[str]:
            return ["deploy"]

        async def broken_run(ctx: object, name: str, log_write: object = None) -> bool:
            msg = "unexpected"
            raise RuntimeError(msg)

        monkeypatch.setattr(orch_mod, "list_effects", fake_list)
        monkeypatch.setattr(orch_mod, "run_effect", broken_run)

        add_commit(upstream, "eff-crash")
        build, _, pool = await run_event(
            postgres_dsn,
            tmp_path,
            upstream,
            FakeEvalRunner([mk_job("a")]),
            FakeExecutor(),
        )
        try:
            assert build is not None
            row = await pool.fetchrow(
                "SELECT status, finished_at FROM build_effects WHERE build_id = $1",
                build.id,
            )
            assert row is not None
            assert row["status"] == "failed"
            assert row["finished_at"] is not None
        finally:
            await pool.close()

    asyncio.run(run())
