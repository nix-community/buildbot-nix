"""Tests for commit status reporting: contexts, caps, success-flip,
stale-generation dropping (with fake posters and in-memory store)."""

# ruff: noqa: PLR2004, ARG002, FBT003, RUF059 (test fakes and literals)

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace

import httpx
import pytest

from nixbot.db import BuildRecord
from nixbot.events import ChangeEvent, RepoInfo
from nixbot.forge import GitlabClient
from nixbot.models import NixEvalJobError
from nixbot.scheduler import AttributeResult, AttributeStatus
from nixbot.status import (
    POSTED_GENERATIONS_MAX,
    ForgeStatusReporter,
    GitlabStatusPoster,
    StatusState,
    attr_status_context,
    eval_description,
)


@dataclass
class Posted:
    sha: str
    context: str
    state: StatusState
    description: str
    target_url: str


@dataclass
class FakePoster:
    posts: list[Posted] = field(default_factory=list)

    async def post(  # noqa: PLR0913
        self,
        owner: str,
        repo: str,
        sha: str,
        context: str,
        state: StatusState,
        description: str,
        target_url: str,
    ) -> None:
        self.posts.append(Posted(sha, context, state, description, target_url))


class MemoryFailedStatuses:
    def __init__(self) -> None:
        self.failed: dict[str, set[str]] = {}

    async def mark_failed(self, revision: str, status_name: str) -> None:
        self.failed.setdefault(revision, set()).add(status_name)

    async def get_failed(self, revision: str) -> set[str]:
        return set(self.failed.get(revision, set()))

    async def clear(self, revision: str, status_name: str) -> None:
        self.failed.get(revision, set()).discard(status_name)


PROJECT = RepoInfo(
    id=1,
    key="github/acme/widget",
    name="acme/widget",
    owner="acme",
    repo="widget",
    forge="github",
    clone_url="https://github.com/acme/widget.git",
    default_branch="main",
)

EVENT = ChangeEvent(repo=PROJECT, branch="main", commit_sha="sha1")

BUILD = BuildRecord(
    id=10,
    project_id=1,
    number=42,
    tree_hash="tree",
    commit_sha="sha1",
    branch="main",
    pr_number=None,
    status="building",
    status_generation=0,
    effects_started=False,
)


def attr_result(
    attr: str, status: AttributeStatus, error: str | None = None
) -> AttributeResult:
    return AttributeResult(
        attr=attr,
        status=status,
        job=NixEvalJobError(error=error or "", attr=attr, attr_path=[attr]),
        error=error,
    )


def make_reporter(
    limit: int = 47,
) -> tuple[ForgeStatusReporter, FakePoster, MemoryFailedStatuses]:
    poster = FakePoster()
    store = MemoryFailedStatuses()
    reporter = ForgeStatusReporter(
        {"github": poster, "gitea": poster},
        store,
        "https://ci.test",
        failed_build_report_limit=limit,
    )
    return reporter, poster, store


def test_posted_generations_bounded() -> None:
    """One entry per build forever is a slow leak in a long-lived
    process; the generation guard only matters short-term."""

    async def run() -> None:
        reporter, _poster, _store = make_reporter()
        for build_id in range(POSTED_GENERATIONS_MAX + 100):
            build = replace(BUILD, id=build_id)
            await reporter.build_finished(EVENT, build, "succeeded", 1, [])
        assert len(reporter._posted_generations) <= POSTED_GENERATIONS_MAX  # noqa: SLF001
        assert (POSTED_GENERATIONS_MAX + 99) in reporter._posted_generations  # noqa: SLF001

    asyncio.run(run())


def test_context_names_unchanged() -> None:
    assert (
        attr_status_context("github", "acme/widget", "x86_64-linux.foo")
        == "buildbot/nix-build github:acme/widget#checks.x86_64-linux.foo"
    )


def test_eval_description_warning_count() -> None:
    assert eval_description(True, []) == "evaluation succeeded"
    assert eval_description(True, ["w"]) == "evaluation succeeded (1 warning)"
    assert eval_description(False, ["a", "b"]) == "evaluation failed (2 warnings)"


def test_phase_statuses_and_target_url() -> None:
    reporter, poster, _ = make_reporter()

    async def run() -> None:
        await reporter.build_started(EVENT, BUILD)
        await reporter.eval_finished(EVENT, BUILD, success=True, warnings=["w"])
        await reporter.build_finished(EVENT, BUILD, "succeeded", 1, [])

    asyncio.run(run())
    contexts = [(p.context, p.state) for p in poster.posts]
    assert contexts == [
        ("buildbot/nix-eval", StatusState.pending),
        ("buildbot/nix-eval", StatusState.success),
        ("buildbot/nix-build", StatusState.pending),
        ("buildbot/nix-build", StatusState.success),
    ]
    assert all(
        p.target_url == "https://ci.test/repos/github/acme/widget/builds/42"
        for p in poster.posts
    )
    assert "(1 warning)" in poster.posts[1].description


def test_per_attribute_failure_statuses_capped() -> None:
    reporter, poster, store = make_reporter(limit=2)
    results = [
        attr_result(f"a{i}", AttributeStatus.failed, error=f"boom {i}")
        for i in range(4)
    ]

    asyncio.run(reporter.build_finished(EVENT, BUILD, "failed", 1, results))
    failure_posts = [
        p for p in poster.posts if p.context.startswith("buildbot/nix-build ")
    ]
    assert len(failure_posts) == 2  # capped at the limit
    # Combined nix-build context still reports the full picture.
    combined = next(p for p in poster.posts if p.context == "buildbot/nix-build")
    assert combined.state == StatusState.failure
    assert "4 of 4" in combined.description


def test_success_flip_on_rebuild() -> None:
    reporter, poster, store = make_reporter()
    context = attr_status_context("github", "acme/widget", "flaky")

    async def run() -> None:
        # First build: flaky fails.
        await reporter.build_finished(
            EVENT, BUILD, "failed", 1, [attr_result("flaky", AttributeStatus.failed)]
        )
        assert context in await store.get_failed("sha1")
        # Rebuild succeeds: status flipped, record cleared.
        await reporter.build_finished(
            EVENT,
            BUILD,
            "succeeded",
            2,
            [attr_result("flaky", AttributeStatus.succeeded)],
        )
        assert context not in await store.get_failed("sha1")

    asyncio.run(run())
    flip = [p for p in poster.posts if p.context == context]
    assert [p.state for p in flip] == [StatusState.failure, StatusState.success]


def test_stale_generation_dropped() -> None:
    reporter, poster, _ = make_reporter()

    async def run() -> None:
        await reporter.build_finished(EVENT, BUILD, "succeeded", 5, [])
        posts_before = len(poster.posts)
        # Stale post with lower generation: dropped entirely.
        await reporter.build_finished(EVENT, BUILD, "failed", 3, [])
        assert len(poster.posts) == posts_before

    asyncio.run(run())


def test_cancelled_attributes_recorded_as_failed_statuses() -> None:
    reporter, poster, store = make_reporter()
    asyncio.run(
        reporter.build_finished(
            EVENT,
            BUILD,
            "cancelled",
            1,
            [attr_result("a", AttributeStatus.cancelled)],
        )
    )
    context = attr_status_context("github", "acme/widget", "a")
    assert context in asyncio.run(store.get_failed("sha1"))
    combined = next(p for p in poster.posts if p.context == "buildbot/nix-build")
    assert combined.state == StatusState.error


def test_attribute_cancel_summary_is_not_superseded() -> None:
    """Cancelling one attribute must aggregate like a failure, not
    claim the whole build was superseded."""
    reporter, poster, _ = make_reporter()
    asyncio.run(
        reporter.build_finished(
            EVENT,
            BUILD,
            "cancelled",
            1,
            [
                attr_result("a", AttributeStatus.cancelled),
                attr_result("b", AttributeStatus.succeeded),
                attr_result("c", AttributeStatus.succeeded),
            ],
        )
    )
    combined = next(p for p in poster.posts if p.context == "buildbot/nix-build")
    assert combined.state == StatusState.error
    assert combined.description == "1 cancelled, 2 succeeded"
    assert "superseded" not in combined.description


def test_build_level_cancel_keeps_supersede_wording() -> None:
    reporter, poster, _ = make_reporter()
    asyncio.run(reporter.build_finished(EVENT, BUILD, "cancelled", 1, []))
    combined = next(p for p in poster.posts if p.context == "buildbot/nix-build")
    assert combined.description == "build cancelled (superseded)"


def test_attribute_descriptions_are_ansi_stripped() -> None:
    """failure_excerpt keeps ANSI colors for the web UI; forge statuses
    must not carry raw escape codes."""
    reporter, poster, _ = make_reporter()
    asyncio.run(
        reporter.build_finished(
            EVENT,
            BUILD,
            "failed",
            1,
            [
                attr_result(
                    "a", AttributeStatus.failed, error="\x1b[31merror: boom\x1b[0m"
                )
            ],
        )
    )
    attr_post = next(
        p for p in poster.posts if p.context.startswith("buildbot/nix-build ")
    )
    assert attr_post.description == "error: boom"


def test_previously_failed_reposts_do_not_consume_budget() -> None:
    """Re-posts of previously-failed contexts must not eat the report
    limit for new failures on a rebuild."""
    reporter, poster, store = make_reporter(limit=2)

    async def run() -> None:
        # First build: a0, a1 fail (consume the full budget).
        await reporter.build_finished(
            EVENT,
            BUILD,
            "failed",
            1,
            [attr_result(f"a{i}", AttributeStatus.failed) for i in range(2)],
        )
        poster.posts.clear()
        # Rebuild: same two still fail, plus one new failure.
        await reporter.build_finished(
            EVENT,
            BUILD,
            "failed",
            2,
            [attr_result(f"a{i}", AttributeStatus.failed) for i in range(3)],
        )

    asyncio.run(run())
    failure_posts = {
        p.context for p in poster.posts if p.context.startswith("buildbot/nix-build ")
    }
    # a2 is reported: the re-posts did not exhaust the budget of 2.
    assert attr_status_context("github", "acme/widget", "a2") in failure_posts


def test_summary_counts_use_all_attribute_statuses() -> None:
    """Reruns pass only the re-run subset as results; the summary
    description must still cover the whole build."""
    reporter, poster, _ = make_reporter()
    all_statuses = {f"ok{i}": "succeeded" for i in range(99)} | {"flaky": "succeeded"}
    asyncio.run(
        reporter.build_finished(
            EVENT,
            BUILD,
            "succeeded",
            1,
            [attr_result("flaky", AttributeStatus.succeeded)],
            attr_statuses=all_statuses,
        )
    )
    combined = next(p for p in poster.posts if p.context == "buildbot/nix-build")
    assert combined.description == "100 attributes built"


def test_attr_prefix_follows_repo_configuration() -> None:
    """Repos with attribute = "hydraJobs" keep their old context names."""
    reporter, poster, store = make_reporter()

    async def run() -> None:
        await reporter.build_finished(
            EVENT,
            BUILD,
            "failed",
            1,
            [attr_result("foo", AttributeStatus.failed)],
            attr_prefix="hydraJobs",
        )

    asyncio.run(run())
    assert any(
        p.context == "buildbot/nix-build github:acme/widget#hydraJobs.foo"
        for p in poster.posts
    )


def test_poster_network_errors_do_not_propagate() -> None:
    """Transport failures on non-terminal posts must not wedge the
    pipeline; the terminal summary propagates to drive the queued
    retry (RetryingReporter catches it)."""

    class ExplodingPoster:
        async def post(self, *args: object, **kwargs: object) -> None:
            msg = "forge unreachable"
            raise httpx.ConnectError(msg)

    store = MemoryFailedStatuses()
    reporter = ForgeStatusReporter({"github": ExplodingPoster()}, store, "https://ci")

    async def run() -> None:
        await reporter.build_started(EVENT, BUILD)  # must not raise
        with pytest.raises(httpx.ConnectError):
            await reporter.build_finished(EVENT, BUILD, "succeeded", 1, [])

    asyncio.run(run())


def test_gitlab_status_states() -> None:
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # raw_path: httpx decodes %2F in .path, hiding a broken encoding.
        assert (
            request.url.raw_path == b"/api/v4/projects/Mic92%2Fdotfiles/statuses/abc123"
        )
        posted.append(json.loads(request.content))
        return httpx.Response(201, json={})

    poster = GitlabStatusPoster(
        GitlabClient(
            "https://gitlab.com",
            "t",
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
    )

    async def run() -> None:
        for state in (StatusState.pending, StatusState.error):
            await poster.post(
                "Mic92", "dotfiles", "abc123", "nix-eval", state, "d" * 300, "u"
            )

    asyncio.run(run())
    assert [p["state"] for p in posted] == ["pending", "failed"]
    assert len(posted[0]["description"]) == 255
