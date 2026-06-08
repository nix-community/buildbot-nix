"""Orchestrator/state-machine tests: ephemeral Postgres,
real git repos, fake eval and executor."""

# ruff: noqa: PLR2004, ARG001, ARG002 (test literals; protocol fakes ignore args)

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import asyncpg
import pytest

from nixbot import orchestrator as orch_mod
from nixbot.config import Config
from nixbot.db import BuildDB, BuildStatus
from nixbot.events import ChangeEvent, RepoInfo
from nixbot.gitrepo import FetchCredentials, RepoManager
from nixbot.memory import EvalWorkerConfig
from nixbot.models import CacheStatus
from nixbot.nix_eval import EvalError, EvalResult, EvalSettings
from nixbot.orchestrator import AttributeExecutor, EvalRunnerLike, Orchestrator
from nixbot.recovery import fail_interrupted_effects
from nixbot.scheduler import (
    AttributeResult,
    AttributeStatus,
    BuildOutcome,
)
from nixbot.work_queue import WorkQueue

from .support import git, insert_project, mk_job

if TYPE_CHECKING:
    from pathlib import Path

    from nixbot.db import BuildRecord
    from nixbot.executor import LogWriter
    from nixbot.models import NixEvalJobSuccess

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git not available"),
    pytest.mark.usefixtures("fresh_work_queue"),
]


# --- fakes --------------------------------------------------------------------


@dataclass
class FakeEvalRunner:
    jobs: list[NixEvalJobSuccess]
    # Emitted as stderr warning lines through on_stderr_line.
    warnings: list[str] = field(default_factory=list)
    calls: int = 0
    last_settings: EvalSettings | None = None
    block: asyncio.Event | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(
        self,
        worktree_path: Path,
        branch_config: object,
        settings: EvalSettings,
        on_jobs: object = None,
        on_stderr_line: object = None,
    ) -> EvalResult:
        self.calls += 1
        self.started.set()
        self.last_settings = settings
        settings.gc_roots_dir.mkdir(parents=True, exist_ok=True)
        if self.block is not None:
            await self.block.wait()
        if callable(on_stderr_line):
            for warning in self.warnings:
                await on_stderr_line(f"warning: {warning}")
        return EvalResult(jobs=list(self.jobs))


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
        log_writer: LogWriter,
        cwd: Path,
        cancel_event: asyncio.Event | None = None,
    ) -> BuildOutcome:
        self.built.append(job.attr)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        await log_writer.write(b"fake build output\n")
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
    project_id = await insert_project(
        pool, name, forge_repo_id=f"id-{name}", url="https://x"
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
    eval_runner: EvalRunnerLike,
    executor: AttributeExecutor,
) -> tuple[Orchestrator, RecordingReporter]:
    config = Config(
        db_url="unused",
        build_systems=["x86_64-linux"],
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
        eval_runner=eval_runner,
        executor=executor,
        reporter=reporter,
        register_gcroot=fake_register,
    )
    return orchestrator, reporter


async def run_event(
    dsn: str,
    tmp_path: Path,
    upstream: Path,
    eval_runner: EvalRunnerLike,
    executor: AttributeExecutor,
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


async def run_effect_build(
    dsn: str,
    tmp_path: Path,
    upstream: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[BuildRecord | None, Orchestrator, RepoInfo, asyncpg.Pool, list[str]]:
    """One default-branch build with a fake "deploy" effect; returns
    the recorded effect runs for further assertions."""
    ran: list[str] = []

    async def fake_list(ctx: object) -> list[str]:
        return ["deploy"]

    async def fake_run(ctx: object, name: str, log_write: object = None) -> bool:
        ran.append(name)
        return True

    monkeypatch.setattr(orch_mod, "list_effects", fake_list)
    monkeypatch.setattr(orch_mod, "run_effect", fake_run)
    pool = await asyncpg.create_pool(dsn)
    orchestrator, _ = make_orchestrator(
        pool, tmp_path, FakeEvalRunner([mk_job("a")]), FakeExecutor()
    )
    project = await make_project(pool)
    project = RepoInfo(**{**project.__dict__, "clone_url": str(upstream)})
    event = ChangeEvent(
        repo=project,
        branch="main",
        commit_sha=git(upstream, "rev-parse", "HEAD"),
    )
    build = await orchestrator.handle_change_event(event)
    if build is not None:
        await drain_effect_items(orchestrator, project, pool)
    return build, orchestrator, project, pool, ran


async def drain_effect_items(
    orchestrator: Orchestrator, info: RepoInfo, pool: asyncpg.Pool
) -> None:
    """Execute queued effect items (the service dispatcher's job)."""
    queue = WorkQueue(pool)
    while (item := await queue.claim_next()) is not None:
        if item.kind == "effect":
            build = await orchestrator.db.get_build(item.payload["build_id"])
            assert build is not None
            await orchestrator.run_effect_item(info, build, item.payload["name"])
        await queue.finish(item.id)


def add_commit(upstream: Path, name: str) -> str:
    (upstream / name).write_text("x")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", name)
    return git(upstream, "rev-parse", "HEAD")


async def make_env(  # noqa: PLR0913
    dsn: str,
    tmp_path: Path,
    upstream: Path,
    eval_runner: EvalRunnerLike,
    executor: AttributeExecutor,
    name: str,
) -> tuple[asyncpg.Pool, Orchestrator, RecordingReporter, RepoInfo]:
    pool = await asyncpg.create_pool(dsn)
    orchestrator, reporter = make_orchestrator(
        pool,
        tmp_path,
        eval_runner,
        executor,
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


def test_main_push_promotes_reused_pr_build(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    """A main push reusing a PR build (identical tree) must shed the
    PR identity and refresh schedules."""

    async def run() -> None:
        add_commit(upstream, "promote")
        sha = git(upstream, "rev-parse", "HEAD")
        eval_runner = FakeEvalRunner([mk_job("a")])
        executor = FakeExecutor()
        pool, orchestrator, _, project = await make_env(
            postgres_dsn, tmp_path, upstream, eval_runner, executor, name="promote"
        )
        try:
            pr_build = await orchestrator.handle_change_event(
                ChangeEvent(repo=project, branch="main", commit_sha=sha, pr_number=7)
            )
            assert pr_build is not None
            assert pr_build.pr_number == 7
            main_build = await orchestrator.handle_change_event(
                ChangeEvent(repo=project, branch="main", commit_sha=sha)
            )
            assert main_build is not None
            assert main_build.id == pr_build.id
            row = await pool.fetchrow(
                "SELECT branch, pr_number FROM builds WHERE id = $1", pr_build.id
            )
            assert dict(row) == {"branch": "main", "pr_number": None}
            assert await pool.fetchval(
                "SELECT count(*) FROM work_queue WHERE kind = 'refresh-schedules'"
            )
        finally:
            await pool.close()

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
        orchestrator.failed_build_cache = lambda _project_id: cache  # type: ignore[assignment]
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


def test_recovery_rerun_failures_not_cached(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    """Jobs reconstructed on recovery have empty dependency closures:
    dependents of one broken drv fail individually and must not enter
    the failed-build cache."""

    class RecordingCache:
        def __init__(self) -> None:
            self.added: list[str] = []

        async def check(self, drv_path: str) -> None:
            return None

        async def add(self, drv_path: str, url: str) -> None:
            self.added.append(drv_path)

    async def run() -> None:
        sha = add_commit(upstream, "recov")
        pool, orchestrator, _, project = await make_env(
            postgres_dsn,
            tmp_path,
            upstream,
            FakeEvalRunner([]),
            FakeExecutor(outcomes={"a": BuildOutcome.failure}),
            "recov",
        )
        cache = RecordingCache()
        orchestrator.failed_build_cache = lambda _project_id: cache  # type: ignore[assignment]
        orchestrator.config = orchestrator.config.model_copy(
            update={"cache_failed_builds": True}
        )
        db = BuildDB(pool)
        build, _ = await db.get_or_create_build(project.id, "recov-tree", sha, "main")
        try:
            await orchestrator.rerun_pending_attributes(project, build, [mk_job("a")])
            assert await db.get_attribute_statuses(build.id) == {"a": "failed"}
            assert cache.added == []
        finally:
            await pool.close()

    asyncio.run(run())


async def make_gitlab_mr_env(
    dsn: str,
    tmp_path: Path,
    upstream: Path,
    eval_runner: EvalRunnerLike,
    name: str,
) -> tuple[asyncpg.Pool, Orchestrator, RepoInfo, str]:
    """GitLab project whose MR head is only reachable via
    refs/merge-requests/7/head. The file:// URL forces the full
    transfer protocol, and the bare clone is created before the MR ref
    exists: a local-path clone would copy all objects regardless of
    the fetch refspec, masking a wrong refspec."""
    pool, orchestrator, _, project = await make_env(
        dsn, tmp_path, upstream, eval_runner, FakeExecutor(), name
    )
    project = RepoInfo(
        **{**project.__dict__, "forge": "gitlab", "clone_url": f"file://{upstream}"}
    )
    await orchestrator.repos.fetch(
        project.key, project.clone_url, ["+refs/heads/*:refs/heads/*"]
    )
    git(upstream, "checkout", "-b", "mrsrc")
    mr_sha = add_commit(upstream, "mr")
    git(upstream, "update-ref", "refs/merge-requests/7/head", mr_sha)
    git(upstream, "checkout", "main")
    git(upstream, "branch", "-D", "mrsrc")
    return pool, orchestrator, project, mr_sha


def test_gitlab_mr_fetches_merge_request_refs(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    """GitLab serves MR heads under refs/merge-requests/*, not the
    GitHub-style refs/pull/*."""

    async def run() -> None:
        pool, orchestrator, project, mr_sha = await make_gitlab_mr_env(
            postgres_dsn, tmp_path, upstream, FakeEvalRunner([mk_job("a")]), "glmr"
        )
        event = ChangeEvent(
            repo=project,
            branch="main",
            commit_sha=mr_sha,
            pr_number=7,
            base_sha="refs/heads/main",
        )
        try:
            build = await orchestrator.handle_change_event(event)
            assert build is not None
            assert await build_status(pool, build.id) == BuildStatus.SUCCEEDED
        finally:
            await pool.close()

    asyncio.run(run())


def test_gitlab_rerun_fetches_merge_request_refs(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    """The rerun path must use the GitLab MR refspec too."""

    async def run() -> None:
        pool, orchestrator, project, mr_sha = await make_gitlab_mr_env(
            postgres_dsn, tmp_path, upstream, FakeEvalRunner([]), "glmrrerun"
        )
        db = BuildDB(pool)
        build, _ = await db.get_or_create_build(
            project.id, "mr-tree", mr_sha, "main", pr_number=7
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


def test_eval_settings_wired(
    postgres_dsn: str,
    tmp_path: Path,
    upstream: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pin the auto-sizing: it reads live memory, which shifts under
    # parallel test load.
    monkeypatch.setattr(
        orch_mod,
        "calculate_eval_workers",
        lambda: EvalWorkerConfig(count=3, max_memory_mib=1234),
    )

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
            assert settings.worker_count == 3
            # Auto-sized workers carry the computed per-worker memory
            # limit, capped by the configured ceiling.
            assert settings.max_memory_size_mib == min(
                orchestrator.config.eval_max_memory_size, 1234
            )
            assert settings.netrc_file == netrc
        finally:
            await pool.close()

    asyncio.run(run())


def test_eval_netrc_withheld_from_pr_with_instance_wide_creds(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    """PR-controlled eval fetches arbitrary flake inputs with the
    netrc; an instance-wide Gitea/GitLab token must not reach it.
    Repo-scoped GitHub tokens may."""

    async def run() -> None:
        git(upstream, "checkout", "-b", "prsrc")
        pr_sha = add_commit(upstream, "prnetrc")
        git(upstream, "update-ref", "refs/pull/9/head", pr_sha)
        pr_sha2 = add_commit(upstream, "prnetrc2")
        git(upstream, "update-ref", "refs/pull/9/head", pr_sha2)
        git(upstream, "checkout", "main")
        git(upstream, "branch", "-D", "prsrc")

        eval_runner = FakeEvalRunner([mk_job("a")])
        pool, orchestrator, _, project = await make_env(
            postgres_dsn, tmp_path, upstream, eval_runner, FakeExecutor(), "prnetrc"
        )
        netrc = tmp_path / "netrc"
        netrc.write_text("")
        try:
            await orchestrator.handle_change_event(
                ChangeEvent(
                    repo=project, branch="main", commit_sha=pr_sha, pr_number=9
                ),
                FetchCredentials(netrc_file=netrc),
            )
            assert eval_runner.last_settings is not None
            assert eval_runner.last_settings.netrc_file is None

            await orchestrator.handle_change_event(
                ChangeEvent(
                    repo=project, branch="main", commit_sha=pr_sha2, pr_number=9
                ),
                FetchCredentials(netrc_file=netrc, repo_scoped=True),
            )
            assert eval_runner.last_settings.netrc_file == netrc
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
        add_commit(upstream, "eff")
        build, _, _, pool, ran = await run_effect_build(
            postgres_dsn, tmp_path, upstream, monkeypatch
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


def test_pr_worktree_config_cannot_grant_effects(
    postgres_dsn: str, tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The effects-gating config must come from the default branch, not
    the PR's merged tree: a PR adding effects_on_pull_requests = true
    to nixbot.toml must not grant itself effects."""

    async def run() -> None:
        git(upstream, "checkout", "-b", "pr")
        (upstream / "nixbot.toml").write_text("effects_on_pull_requests = true\n")
        git(upstream, "add", ".")
        git(upstream, "commit", "-m", "grant myself effects")
        head = git(upstream, "rev-parse", "HEAD")
        git(upstream, "checkout", "main")
        base = git(upstream, "rev-parse", "HEAD")

        ran: list[str] = []

        async def fake_list(ctx: object) -> list[str]:
            return ["deploy"]

        async def fake_run(ctx: object, name: str, log_write: object = None) -> bool:
            ran.append(name)
            return True

        monkeypatch.setattr(orch_mod, "list_effects", fake_list)
        monkeypatch.setattr(orch_mod, "run_effect", fake_run)
        pool, orchestrator, _, project = await make_env(
            postgres_dsn,
            tmp_path,
            upstream,
            FakeEvalRunner([mk_job("a")]),
            FakeExecutor(),
            name="pr-grant",
        )
        try:
            build = await orchestrator.handle_change_event(
                ChangeEvent(
                    repo=project,
                    branch="main",  # PR base ref, as webhooks report it
                    commit_sha=head,
                    pr_number=9,
                    base_sha=base,
                )
            )
            assert build is not None
            await drain_effect_items(orchestrator, project, pool)
            assert ran == []
            assert not await pool.fetchval(
                "SELECT effects_started FROM builds WHERE id = $1", build.id
            )
        finally:
            await pool.close()

    asyncio.run(run())


def test_rerun_effects_runs_effects_again(
    postgres_dsn: str, tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Effects-only restart: re-runs effects from a fresh worktree
    without touching the build's attributes."""

    async def run() -> None:
        add_commit(upstream, "eff2")
        build, orchestrator, project, pool, ran = await run_effect_build(
            postgres_dsn, tmp_path, upstream, monkeypatch
        )
        try:
            assert build is not None
            assert ran == ["deploy"]
            attrs_before = await pool.fetch(
                "SELECT attr, status, finished_at FROM build_attributes "
                "WHERE build_id = $1",
                build.id,
            )
            # A stale row from an effect no longer in the flake.
            await pool.execute(
                "INSERT INTO build_effects (build_id, name, status) "
                "VALUES ($1, 'removed', 'failed')",
                build.id,
            )
            await orchestrator.rerun_effects(project, build)
            await drain_effect_items(orchestrator, project, pool)
            assert ran == ["deploy", "deploy"]
            # Restarts must queue a schedule refresh too.
            assert await pool.fetchval(
                "SELECT count(*) FROM work_queue WHERE kind = 'refresh-schedules'"
            )
            rows = await pool.fetch(
                "SELECT name, status FROM build_effects WHERE build_id = $1",
                build.id,
            )
            assert [(r["name"], r["status"]) for r in rows] == [("deploy", "succeeded")]
            attrs_after = await pool.fetch(
                "SELECT attr, status, finished_at FROM build_attributes "
                "WHERE build_id = $1",
                build.id,
            )
            assert attrs_before == attrs_after
        finally:
            await pool.close()

    asyncio.run(run())


def test_recovery_rerun_runs_effects(
    postgres_dsn: str, tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build recovered after a crash that happened before effects
    started must still run them on success."""

    async def run() -> None:
        add_commit(upstream, "rec")
        build, orchestrator, project, pool, ran = await run_effect_build(
            postgres_dsn, tmp_path, upstream, monkeypatch
        )
        try:
            assert build is not None
            assert ran == ["deploy"]
            # Simulate a crash before effects started.
            await pool.execute(
                "UPDATE builds SET effects_started = FALSE WHERE id = $1", build.id
            )
            await pool.execute(
                "UPDATE build_attributes SET status = 'pending' WHERE build_id = $1",
                build.id,
            )
            await orchestrator.rerun_pending_attributes(project, build, [mk_job("a")])
            await drain_effect_items(orchestrator, project, pool)
            assert ran == ["deploy", "deploy"]
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


def test_effect_items_resume_only_pending(
    postgres_dsn: str, tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a crash, pending effects re-run via their queued items;
    a row caught mid-run (swept to failed) must not re-deploy."""

    async def run() -> None:
        add_commit(upstream, "eff-resume")
        build, orchestrator, project, pool, ran = await run_effect_build(
            postgres_dsn, tmp_path, upstream, monkeypatch
        )
        try:
            assert build is not None
            assert ran == ["deploy"]
            # Crash mid-run: the sweep settles the row; the requeued
            # item must skip it.
            await pool.execute(
                "UPDATE build_effects SET status = 'running' WHERE build_id = $1",
                build.id,
            )
            await fail_interrupted_effects(
                pool, datetime.now(UTC) + timedelta(minutes=1)
            )
            await orchestrator.run_effect_item(project, build, "deploy")
            assert ran == ["deploy"]
            # Crash before the run: the pending row resumes.
            await pool.execute(
                "UPDATE build_effects SET status = 'pending' WHERE build_id = $1",
                build.id,
            )
            await orchestrator.run_effect_item(project, build, "deploy")
            assert ran == ["deploy", "deploy"]
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
        pool, orchestrator, _, project = await make_env(
            postgres_dsn,
            tmp_path,
            upstream,
            FakeEvalRunner([mk_job("a")]),
            FakeExecutor(),
            name="eff-crash",
        )
        event = ChangeEvent(
            repo=project,
            branch="main",
            commit_sha=git(upstream, "rev-parse", "HEAD"),
        )
        build = await orchestrator.handle_change_event(event)
        await drain_effect_items(orchestrator, project, pool)
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


def test_post_process_error_does_not_wedge_build(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    """A gcroot/outputs failure after the attributes settled must not
    leave the build stuck in 'building' without a final status."""

    async def run() -> None:
        add_commit(upstream, "pp-err")
        eval_runner = FakeEvalRunner(
            [mk_job("a", cache_status=CacheStatus.local, out="/nix/store/a-out")]
        )
        executor = FakeExecutor()
        pool, orchestrator, reporter, project = await make_env(
            postgres_dsn, tmp_path, upstream, eval_runner, executor, name="pp-err"
        )

        async def boom(gcroots_dir: Path, proj: str, attr: str, out: str) -> None:
            msg = "disk full"
            raise OSError(msg)

        orchestrator.register_gcroot = boom
        try:
            build = await orchestrator.handle_change_event(
                ChangeEvent(
                    repo=project,
                    branch="main",
                    commit_sha=git(upstream, "rev-parse", "HEAD"),
                )
            )
            assert build is not None
            row = await pool.fetchrow(
                "SELECT status, error FROM builds WHERE id = $1", build.id
            )
            assert row["status"] == BuildStatus.FAILED
            assert "disk full" in row["error"]
            finished = [e for e in reporter.events if e[0] == "finished"]
            assert finished
            assert finished[-1][2] == BuildStatus.FAILED
            # The eval succeeded; only the build may turn red.
            assert ("eval", build.id, False, ()) not in reporter.events
        finally:
            await pool.close()

    asyncio.run(run())


def test_unexpected_eval_path_error_settles_build(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    """Any exception on the eval path (not just EvalError) must settle
    the build as failed instead of wedging it in 'evaluating' and
    leaking the build task blocked on the jobs queue."""

    @dataclass
    class BoomEvalRunner:
        async def run(
            self,
            worktree_path: Path,
            branch_config: object,
            settings: EvalSettings,
            on_jobs: object = None,
            on_stderr_line: object = None,
        ) -> EvalResult:
            msg = "db outage"
            raise RuntimeError(msg)

    async def run() -> None:
        add_commit(upstream, "eval-boom")
        pool, orchestrator, reporter, project = await make_env(
            postgres_dsn,
            tmp_path,
            upstream,
            BoomEvalRunner(),
            FakeExecutor(),
            name="eval-boom",
        )
        try:
            build = await orchestrator.handle_change_event(
                ChangeEvent(
                    repo=project,
                    branch="main",
                    commit_sha=git(upstream, "rev-parse", "HEAD"),
                )
            )
            assert build is not None
            row = await pool.fetchrow(
                "SELECT status, error FROM builds WHERE id = $1", build.id
            )
            assert row["status"] == BuildStatus.FAILED
            assert "db outage" in row["error"]
            assert ("eval", build.id, False, ()) in reporter.events
            assert reporter.events[-1][2] == BuildStatus.FAILED
            # No leaked consumer task blocked on the jobs queue.
            leaked = [
                t
                for t in asyncio.all_tasks()
                if t is not asyncio.current_task() and not t.done()
            ]
            assert leaked == []
        finally:
            await pool.close()

    asyncio.run(run())


def test_reuse_empty_succeeded_build_posts_eval_success(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    """Reusing a genuinely empty-but-green build must not report
    'evaluation failed' on the new commit."""

    async def run() -> None:
        add_commit(upstream, "empty-green")
        eval_runner = FakeEvalRunner([])
        executor = FakeExecutor()
        pool, orchestrator, reporter, project = await make_env(
            postgres_dsn, tmp_path, upstream, eval_runner, executor, name="empty-green"
        )
        try:
            sha = git(upstream, "rev-parse", "HEAD")
            build1 = await orchestrator.handle_change_event(
                ChangeEvent(repo=project, branch="main", commit_sha=sha)
            )
            assert build1 is not None
            assert await build_status(pool, build1.id) == BuildStatus.SUCCEEDED
            build2 = await orchestrator.handle_change_event(
                ChangeEvent(repo=project, branch="main", commit_sha=sha, pr_number=3)
            )
            assert build2 is not None
            assert build2.id == build1.id
            evals = [e for e in reporter.events if e[0] == "eval"]
            assert evals[-1][2] is True
        finally:
            await pool.close()

    asyncio.run(run())


def test_stale_event_reusing_old_build_does_not_cancel_newer(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    """A redelivered push event for an ancestor commit whose tree
    matches an old terminal build must not supersede the in-flight
    newer build."""

    async def run() -> None:
        sha1 = add_commit(upstream, "stale-1")
        eval_runner1 = FakeEvalRunner([mk_job("a")])
        executor = FakeExecutor()
        pool, orchestrator, _, project = await make_env(
            postgres_dsn, tmp_path, upstream, eval_runner1, executor, name="stale"
        )
        try:
            build1 = await orchestrator.handle_change_event(
                ChangeEvent(repo=project, branch="main", commit_sha=sha1)
            )
            assert build1 is not None
            sha2 = add_commit(upstream, "stale-2")
            eval_runner2 = FakeEvalRunner([mk_job("a")], block=asyncio.Event())
            orchestrator.eval_runner = eval_runner2
            task2 = asyncio.create_task(
                orchestrator.handle_change_event(
                    ChangeEvent(repo=project, branch="main", commit_sha=sha2)
                )
            )
            await eval_runner2.started.wait()
            await orchestrator.handle_change_event(
                ChangeEvent(repo=project, branch="main", commit_sha=sha1)
            )
            assert eval_runner2.block is not None
            eval_runner2.block.set()
            build2 = await task2
            assert build2 is not None
            assert await build_status(pool, build2.id) == BuildStatus.SUCCEEDED
        finally:
            await pool.close()

    asyncio.run(run())


def test_reuse_for_default_branch_push_runs_effects(
    postgres_dsn: str, tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A main push reusing a succeeded PR build must still deploy:
    the PR run never started effects."""

    ran: list[str] = []

    async def fake_list(ctx: object) -> list[str]:
        return ["deploy"]

    async def fake_run(ctx: object, name: str, log_write: object = None) -> bool:
        ran.append(name)
        return True

    async def run() -> None:
        monkeypatch.setattr(orch_mod, "list_effects", fake_list)
        monkeypatch.setattr(orch_mod, "run_effect", fake_run)
        add_commit(upstream, "reuse-deploy")
        sha = git(upstream, "rev-parse", "HEAD")
        pool, orchestrator, _, project = await make_env(
            postgres_dsn,
            tmp_path,
            upstream,
            FakeEvalRunner([mk_job("a")]),
            FakeExecutor(),
            name="reuse-deploy",
        )
        try:
            pr_build = await orchestrator.handle_change_event(
                ChangeEvent(repo=project, branch="main", commit_sha=sha, pr_number=9)
            )
            assert pr_build is not None
            await drain_effect_items(orchestrator, project, pool)
            assert ran == []
            main_build = await orchestrator.handle_change_event(
                ChangeEvent(repo=project, branch="main", commit_sha=sha)
            )
            assert main_build is not None
            assert main_build.id == pr_build.id
            await drain_effect_items(orchestrator, project, pool)
            assert ran == ["deploy"]
        finally:
            await pool.close()

    asyncio.run(run())


def test_attach_to_already_terminal_build_replays_status(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    """An event attaching to a build that turned terminal between the
    record fetch and the attach must get the final status replayed,
    not stay pending forever."""

    async def run() -> None:
        add_commit(upstream, "attach-late")
        sha = git(upstream, "rev-parse", "HEAD")
        pool, orchestrator, reporter, project = await make_env(
            postgres_dsn,
            tmp_path,
            upstream,
            FakeEvalRunner([mk_job("a")]),
            FakeExecutor(),
            name="attach-late",
        )
        try:
            build = await orchestrator.handle_change_event(
                ChangeEvent(repo=project, branch="main", commit_sha=sha)
            )
            assert build is not None
            assert await build_status(pool, build.id) == BuildStatus.SUCCEEDED
            # Simulate the race: record fetched before completion,
            # cancel event still registered (build "in flight").
            stale_record = replace(build, status=BuildStatus.BUILDING)
            orchestrator.cancel_events[build.id] = asyncio.Event()
            reporter.events.clear()
            event2 = ChangeEvent(
                repo=project, branch="main", commit_sha=sha, pr_number=11
            )
            await orchestrator._dispatch_build(  # noqa: SLF001
                event2,
                stale_record,
                created=False,
                tree_hash=build.tree_hash or "",
                worktree_path=tmp_path,
                credentials=None,
            )
            finished = [e for e in reporter.events if e[0] == "finished"]
            assert finished
            assert finished[-1][2] == BuildStatus.SUCCEEDED
        finally:
            orchestrator.cancel_events.clear()
            await pool.close()

    asyncio.run(run())


def test_cancel_during_eval_resolves_linked_eval_contexts(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    """Cancelling a build mid-eval must resolve the nix-eval context of
    every linked event, not only the primary one."""

    async def run() -> None:
        add_commit(upstream, "linked-cancel")
        sha = git(upstream, "rev-parse", "HEAD")
        eval_runner = FakeEvalRunner([mk_job("a")], block=asyncio.Event())
        pool, orchestrator, reporter, project = await make_env(
            postgres_dsn,
            tmp_path,
            upstream,
            eval_runner,
            FakeExecutor(),
            name="linked-cancel",
        )
        try:
            task1 = asyncio.create_task(
                orchestrator.handle_change_event(
                    ChangeEvent(repo=project, branch="main", commit_sha=sha)
                )
            )
            await eval_runner.started.wait()
            build2 = await orchestrator.handle_change_event(
                ChangeEvent(repo=project, branch="main", commit_sha=sha, pr_number=4)
            )
            assert build2 is not None
            orchestrator.cancel_events[build2.id].set()
            await task1
            cancelled_evals = [e for e in reporter.events if e[0] == "eval-cancelled"]
            assert len(cancelled_evals) == 2
            finished = [e for e in reporter.events if e[0] == "finished"]
            assert len(finished) == 2
            assert all(e[2] == BuildStatus.CANCELLED for e in finished)
        finally:
            await pool.close()

    asyncio.run(run())


def test_effect_log_does_not_collide_with_attribute_log(
    postgres_dsn: str, tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An attribute named "effect-deploy" and an effect named "deploy"
    must not share one log file."""

    async def fake_list(ctx: object) -> list[str]:
        return ["deploy"]

    async def fake_run(ctx: object, name: str, log_write: object = None) -> bool:
        return True

    async def run() -> None:
        monkeypatch.setattr(orch_mod, "list_effects", fake_list)
        monkeypatch.setattr(orch_mod, "run_effect", fake_run)
        add_commit(upstream, "log-collide")
        pool, orchestrator, _, project = await make_env(
            postgres_dsn,
            tmp_path,
            upstream,
            FakeEvalRunner([mk_job("effect-deploy")]),
            FakeExecutor(),
            name="log-collide",
        )
        try:
            build = await orchestrator.handle_change_event(
                ChangeEvent(
                    repo=project,
                    branch="main",
                    commit_sha=git(upstream, "rev-parse", "HEAD"),
                )
            )
            assert build is not None
            await drain_effect_items(orchestrator, project, pool)
            attr_log = await pool.fetchval(
                "SELECT l.path FROM logs l JOIN build_attributes a "
                "ON l.attribute_id = a.id WHERE a.build_id = $1",
                build.id,
            )
            effect_log = await pool.fetchval(
                "SELECT log_path FROM build_effects WHERE build_id = $1",
                build.id,
            )
            assert attr_log is not None
            assert effect_log is not None
            assert attr_log != effect_log
        finally:
            await pool.close()

    asyncio.run(run())


def test_post_process_paths_are_forge_scoped(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    """Gcroots and outputs for the same owner/repo on two forges must
    not collide; the forge belongs in both path schemes."""

    async def run() -> None:
        add_commit(upstream, "forge-scope")
        pool, orchestrator, _, project = await make_env(
            postgres_dsn,
            tmp_path,
            upstream,
            FakeEvalRunner(
                [mk_job("a", cache_status=CacheStatus.local, out="/nix/store/a-out")]
            ),
            FakeExecutor(),
            name="forge-scope",
        )
        gcroot_projects: list[str] = []
        output_calls: list[tuple] = []

        async def capture_gcroot(
            gcroots_dir: Path, proj: str, attr: str, out: str
        ) -> None:
            gcroot_projects.append(proj)

        def capture_output(*args: object) -> Path:
            output_calls.append(args)
            return tmp_path / "out"

        orchestrator.register_gcroot = capture_gcroot
        orchestrator.write_output_path = capture_output
        orchestrator.config.outputs_path = tmp_path / "outputs"
        try:
            build = await orchestrator.handle_change_event(
                ChangeEvent(
                    repo=project,
                    branch="main",
                    commit_sha=git(upstream, "rev-parse", "HEAD"),
                )
            )
            assert build is not None
            assert gcroot_projects == [project.key]
            assert output_calls
            assert "github" in output_calls[0]
        finally:
            await pool.close()

    asyncio.run(run())


def test_effects_phase_error_keeps_succeeded_build(
    postgres_dsn: str, tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception after the final fan-out (effects discovery here)
    must not flip an already-succeeded build to failed."""

    async def boom_list(ctx: object) -> list[str]:
        msg = "state api down"
        raise RuntimeError(msg)

    async def run() -> None:
        monkeypatch.setattr(orch_mod, "list_effects", boom_list)
        add_commit(upstream, "late-boom")
        pool, orchestrator, reporter, project = await make_env(
            postgres_dsn,
            tmp_path,
            upstream,
            FakeEvalRunner([mk_job("a")]),
            FakeExecutor(),
            name="late-boom",
        )
        try:
            build = await orchestrator.handle_change_event(
                ChangeEvent(
                    repo=project,
                    branch="main",
                    commit_sha=git(upstream, "rev-parse", "HEAD"),
                )
            )
            assert build is not None
            assert await build_status(pool, build.id) == BuildStatus.SUCCEEDED
            finished = [e for e in reporter.events if e[0] == "finished"]
            assert finished[-1][2] == BuildStatus.SUCCEEDED
        finally:
            await pool.close()

    asyncio.run(run())


def test_reuse_post_process_error_still_reports_status(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> None:
    """A gcroot/outputs failure on the reuse path must not strand the
    new context without a final status (same wedge as in-build
    post-processing)."""

    async def run() -> None:
        add_commit(upstream, "reuse-pp-err")
        sha = git(upstream, "rev-parse", "HEAD")
        pool, orchestrator, reporter, project = await make_env(
            postgres_dsn,
            tmp_path,
            upstream,
            FakeEvalRunner([mk_job("a")]),
            FakeExecutor(),
            name="reuse-pp-err",
        )
        try:
            build1 = await orchestrator.handle_change_event(
                ChangeEvent(repo=project, branch="main", commit_sha=sha)
            )
            assert build1 is not None

            async def boom(gcroots_dir: Path, proj: str, attr: str, out: str) -> None:
                msg = "disk full"
                raise OSError(msg)

            orchestrator.register_gcroot = boom
            reporter.events.clear()
            build2 = await orchestrator.handle_change_event(
                ChangeEvent(repo=project, branch="main", commit_sha=sha)
            )
            assert build2 is not None
            finished = [e for e in reporter.events if e[0] == "finished"]
            assert finished
            assert finished[-1][2] == BuildStatus.SUCCEEDED
        finally:
            await pool.close()

    asyncio.run(run())
