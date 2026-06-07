"""Tests for the engine job scheduler, enumerating the behavioral
branches of buildbot_nix/build_trigger.py."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from buildbot_nix.models import (
    CacheStatus,
    NixEvalJob,
    NixEvalJobError,
    NixEvalJobSuccess,
)
from buildbot_nix.scheduler import (
    AttributeResult,
    AttributeStatus,
    BuildOutcome,
    CachedFailure,
    JobScheduler,
    compute_job_closures,
    get_failed_dependents,
    sort_jobs_by_closures,
)

SYSTEM = "x86_64-linux"


def mk_job(
    attr: str,
    *,
    deps: list[str] | None = None,
    cache_status: CacheStatus = CacheStatus.not_built,
    system: str = SYSTEM,
    out: str | None = None,
) -> NixEvalJobSuccess:
    drv = f"/nix/store/{attr}.drv"
    return NixEvalJobSuccess(
        attr=attr,
        attr_path=attr.split("."),
        cache_status=cache_status,
        needed_builds=[drv, *(f"/nix/store/{d}.drv" for d in (deps or []))],
        needed_substitutes=[],
        drv_path=drv,
        name=attr,
        outputs={"out": out or f"/nix/store/{attr}-out"},
        system=system,
    )


class FakeExecutor:
    """Records build order; per-attr outcomes configurable."""

    def __init__(self, outcomes: dict[str, BuildOutcome] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.built: list[str] = []

    async def build(self, job: NixEvalJobSuccess) -> BuildOutcome:
        self.built.append(job.attr)
        await asyncio.sleep(0)
        return self.outcomes.get(job.attr, BuildOutcome.success)


class FakeCache:
    def __init__(self, entries: dict[str, CachedFailure] | None = None) -> None:
        self.entries = entries or {}
        self.added: list[tuple[str, str]] = []
        self.removed: list[str] = []

    async def check(self, drv_path: str) -> CachedFailure | None:
        return self.entries.get(drv_path)

    async def add(self, drv_path: str, url: str) -> None:
        self.added.append((drv_path, url))

    async def remove(self, drv_path: str) -> None:
        self.removed.append(drv_path)
        self.entries.pop(drv_path, None)


def run_scheduler(scheduler: JobScheduler, jobs: list) -> object:
    return asyncio.run(scheduler.run(jobs))


def by_attr(result: object) -> dict[str, AttributeStatus]:
    return {r.attr: r.status for r in result.results}  # type: ignore[attr-defined]


def test_topological_order() -> None:
    # c depends on b depends on a.
    a, b, c = mk_job("a"), mk_job("b", deps=["a"]), mk_job("c", deps=["b"])
    closures = compute_job_closures([c, a, b])
    assert closures[c.drv_path] == {b.drv_path}
    assert closures[b.drv_path] == {a.drv_path}
    order = sort_jobs_by_closures([c, a, b], closures)
    assert [j.attr for j in order] == ["a", "b", "c"]

    executor = FakeExecutor()
    result = run_scheduler(JobScheduler(executor, [SYSTEM]), [c, a, b])
    assert executor.built.index("a") < executor.built.index("b")
    assert executor.built.index("b") < executor.built.index("c")
    assert result.success  # type: ignore[attr-defined]


def test_get_failed_dependents_transitive() -> None:
    a, b, c, d = (
        mk_job("a"),
        mk_job("b", deps=["a"]),
        mk_job("c", deps=["b"]),
        mk_job("d"),
    )
    closures = compute_job_closures([a, b, c, d])
    removed = get_failed_dependents(a, [b, c, d], closures)
    assert {j.attr for j in removed} == {"b", "c"}


def test_dependency_failure_propagation() -> None:
    a, b, c, d = (
        mk_job("a"),
        mk_job("b", deps=["a"]),
        mk_job("c", deps=["b"]),
        mk_job("d"),
    )
    executor = FakeExecutor({"a": BuildOutcome.failure})
    result = run_scheduler(JobScheduler(executor, [SYSTEM]), [a, b, c, d])
    statuses = by_attr(result)
    assert statuses["a"] == AttributeStatus.failed
    assert statuses["b"] == AttributeStatus.dependency_failed
    assert statuses["c"] == AttributeStatus.dependency_failed
    assert statuses["d"] == AttributeStatus.succeeded
    assert "b" not in executor.built
    assert "c" not in executor.built
    deps = {r.attr: r.dependency_attr for r in result.results}  # type: ignore[attr-defined]
    assert deps["b"] == "a"
    assert deps["c"] == "a"
    assert not result.success  # type: ignore[attr-defined]


def test_supported_systems_filter() -> None:
    native = mk_job("native")
    foreign = mk_job("foreign", system="riscv64-linux")
    executor = FakeExecutor()
    result = run_scheduler(JobScheduler(executor, [SYSTEM]), [native, foreign])
    assert executor.built == ["native"]
    assert "foreign" not in by_attr(result)


def test_local_jobs_skipped_with_out_path_recorded() -> None:
    local = mk_job("local", cache_status=CacheStatus.local)
    executor = FakeExecutor()
    result = run_scheduler(JobScheduler(executor, [SYSTEM]), [local])
    assert executor.built == []
    assert by_attr(result)["local"] == AttributeStatus.skipped_local
    assert result.skipped_out_paths == [("local", "/nix/store/local-out")]  # type: ignore[attr-defined]
    assert result.success  # type: ignore[attr-defined]


def test_local_job_unblocks_dependents() -> None:
    base = mk_job("base", cache_status=CacheStatus.local)
    top = mk_job("top", deps=["base"])
    executor = FakeExecutor()
    result = run_scheduler(JobScheduler(executor, [SYSTEM]), [base, top])
    assert executor.built == ["top"]
    assert result.success  # type: ignore[attr-defined]


def test_cached_jobs_scheduled_for_substitution() -> None:
    cached = mk_job("cached", cache_status=CacheStatus.cached)
    executor = FakeExecutor()
    run_scheduler(JobScheduler(executor, [SYSTEM]), [cached])
    assert executor.built == ["cached"]


def test_failed_eval_records() -> None:
    bad = NixEvalJobError(error="boom", attr="bad", attr_path=["bad"])
    good = mk_job("good")
    executor = FakeExecutor()
    result = run_scheduler(JobScheduler(executor, [SYSTEM]), [bad, good])
    statuses = by_attr(result)
    assert statuses["bad"] == AttributeStatus.failed_eval
    assert statuses["good"] == AttributeStatus.succeeded
    errors = {r.attr: r.error for r in result.results}  # type: ignore[attr-defined]
    assert errors["bad"] == "boom"
    assert not result.success  # type: ignore[attr-defined]


def test_cached_failure_skip() -> None:
    job = mk_job("flaky")
    cache = FakeCache(
        {
            job.drv_path: CachedFailure(
                drv_path=job.drv_path,
                time=datetime(2026, 1, 1, tzinfo=UTC),
                url="http://ci/builds/1",
            )
        }
    )
    executor = FakeExecutor()
    result = run_scheduler(
        JobScheduler(executor, [SYSTEM], failed_build_cache=cache), [job]
    )
    assert executor.built == []
    record = result.results[0]  # type: ignore[attr-defined]
    assert record.status == AttributeStatus.cached_failure
    assert record.first_failure_url == "http://ci/builds/1"
    assert not result.success  # type: ignore[attr-defined]


def test_cached_failure_propagates_to_dependents() -> None:
    base = mk_job("base")
    top = mk_job("top", deps=["base"])
    cache = FakeCache(
        {
            base.drv_path: CachedFailure(
                drv_path=base.drv_path, time=datetime(2026, 1, 1, tzinfo=UTC), url="u"
            )
        }
    )
    executor = FakeExecutor()
    result = run_scheduler(
        JobScheduler(executor, [SYSTEM], failed_build_cache=cache), [base, top]
    )
    statuses = by_attr(result)
    assert statuses["base"] == AttributeStatus.cached_failure
    assert statuses["top"] == AttributeStatus.dependency_failed
    assert executor.built == []


def test_rebuild_clears_cached_failure_and_builds() -> None:
    job = mk_job("flaky")
    cache = FakeCache(
        {
            job.drv_path: CachedFailure(
                drv_path=job.drv_path, time=datetime(2026, 1, 1, tzinfo=UTC), url="u"
            )
        }
    )
    executor = FakeExecutor()
    result = run_scheduler(
        JobScheduler(executor, [SYSTEM], failed_build_cache=cache, is_rebuild=True),
        [job],
    )
    assert executor.built == ["flaky"]
    assert cache.removed == [job.drv_path]
    assert result.success  # type: ignore[attr-defined]


def test_failure_recorded_in_cache() -> None:
    job = mk_job("breaks")
    cache = FakeCache()
    executor = FakeExecutor({"breaks": BuildOutcome.failure})
    run_scheduler(
        JobScheduler(
            executor, [SYSTEM], failed_build_cache=cache, build_url="http://ci/b/2"
        ),
        [job],
    )
    assert cache.added == [(job.drv_path, "http://ci/b/2")]


def test_cancelled_not_recorded_in_cache_and_propagates_cancelled() -> None:
    a = mk_job("a")
    b = mk_job("b", deps=["a"])
    cache = FakeCache()
    executor = FakeExecutor({"a": BuildOutcome.cancelled})
    result = run_scheduler(
        JobScheduler(executor, [SYSTEM], failed_build_cache=cache), [a, b]
    )
    assert cache.added == []
    statuses = by_attr(result)
    assert statuses["a"] == AttributeStatus.cancelled
    # Cancelled propagates "cancelled/skipped", never dependency_failed.
    assert statuses["b"] == AttributeStatus.cancelled


def test_force_attr_builds_local_job() -> None:
    local = mk_job("local", cache_status=CacheStatus.local)
    executor = FakeExecutor()
    result = run_scheduler(
        JobScheduler(executor, [SYSTEM], force_attrs={"local"}), [local]
    )
    assert executor.built == ["local"]
    assert by_attr(result)["local"] == AttributeStatus.succeeded


def test_mixed_eval_results_independent() -> None:
    bad = NixEvalJobError(error="nope", attr="bad", attr_path=["bad"])
    a = mk_job("a")
    b = mk_job("b", deps=["a"])
    executor = FakeExecutor()
    result = run_scheduler(JobScheduler(executor, [SYSTEM]), [bad, a, b])
    statuses = by_attr(result)
    assert statuses == {
        "bad": AttributeStatus.failed_eval,
        "a": AttributeStatus.succeeded,
        "b": AttributeStatus.succeeded,
    }
    assert not result.success  # type: ignore[attr-defined]


def test_skipped_local_frees_dependents_immediately() -> None:
    # parent is already in the local store (skipped); its dependent must
    # be dispatched right away, not only after the unrelated running
    # build finishes.
    parent = mk_job("parent", cache_status=CacheStatus.local)
    child = mk_job("child", deps=["parent"])
    other = mk_job("other")

    started_child = asyncio.Event()

    class BlockingExecutor(FakeExecutor):
        async def build(self, job: NixEvalJobSuccess) -> BuildOutcome:
            self.built.append(job.attr)
            if job.attr == "other":
                # Holds a slot until the child has been dispatched.
                await asyncio.wait_for(started_child.wait(), timeout=5)
            elif job.attr == "child":
                started_child.set()
            return BuildOutcome.success

    async def run() -> object:
        executor = BlockingExecutor()
        return await asyncio.wait_for(
            JobScheduler(executor, [SYSTEM]).run([other, parent, child]), timeout=5
        )

    result = asyncio.run(run())
    statuses = by_attr(result)
    assert statuses["parent"] == AttributeStatus.skipped_local
    assert statuses["child"] == AttributeStatus.succeeded
    assert statuses["other"] == AttributeStatus.succeeded


def test_post_build_failure_not_cached_and_dependents_build() -> None:
    # A post-build-step failure fails the attribute but must not poison
    # the failed-build cache (the derivation built fine) nor fail
    # dependents.
    parent = mk_job("parent")
    child = mk_job("child", deps=["parent"])
    cache = FakeCache()
    executor = FakeExecutor({"parent": BuildOutcome.post_build_failure})
    result = run_scheduler(
        JobScheduler(executor, [SYSTEM], failed_build_cache=cache), [parent, child]
    )
    statuses = by_attr(result)
    assert statuses["parent"] == AttributeStatus.failed
    assert statuses["child"] == AttributeStatus.succeeded
    assert "child" in executor.built
    assert cache.added == []


def test_cached_failure_error_includes_url() -> None:
    job = mk_job("a")
    cached = CachedFailure(
        drv_path=job.drv_path,
        time=datetime(2026, 1, 1, tzinfo=UTC),
        url="https://ci.example/builds/1",
    )
    cache = FakeCache({job.drv_path: cached})
    result = run_scheduler(
        JobScheduler(FakeExecutor(), [SYSTEM], failed_build_cache=cache), [job]
    )
    (res,) = result.results  # type: ignore[attr-defined]
    assert res.status == AttributeStatus.cached_failure
    assert "https://ci.example/builds/1" in res.error


def test_on_result_reports_skips_before_builds_finish() -> None:
    """on_result fires for skips while other jobs are still running."""
    reported: list[tuple[str, str]] = []
    skip_seen = asyncio.Event()

    async def on_result(result: AttributeResult) -> None:
        reported.append((result.attr, result.status.value))
        if result.status == AttributeStatus.skipped_local:
            skip_seen.set()

    class BlockingExecutor(FakeExecutor):
        async def build(self, job: NixEvalJobSuccess) -> BuildOutcome:
            # Deadlocks unless the skip was reported before builds finish.
            await asyncio.wait_for(skip_seen.wait(), timeout=5)
            return await super().build(job)

    scheduler = JobScheduler(BlockingExecutor(), [SYSTEM], on_result=on_result)
    jobs: list[NixEvalJob] = [
        mk_job("local", cache_status=CacheStatus.local),
        mk_job("fresh", cache_status=CacheStatus.not_built),
    ]
    result = asyncio.run(scheduler.run(jobs))
    assert ("local", "skipped_local") in reported
    assert ("fresh", "succeeded") in reported
    assert {r.attr for r in result.results} == {"local", "fresh"}
