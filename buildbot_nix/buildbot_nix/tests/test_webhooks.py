"""Webhook ingestion tests: signatures, dedupe, parsing, fail-fast."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from buildbot_nix.config import BranchConfig, BranchConfigDict
from buildbot_nix.webhooks import (
    ChangeRequest,
    DeliveryDeduper,
    PrClosed,
    WebhookEvent,
    create_webhook_router,
    is_merge_queue_branch,
    parse_gitea_event,
    parse_github_event,
    parse_gitlab_event,
    should_build_branch,
    verify_github_signature,
)

SECRET = "hook-secret"  # noqa: S105


def sign_github(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def sign_gitea(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


# --- pure helpers -----------------------------------------------------------


def test_merge_queue_branches() -> None:
    assert is_merge_queue_branch("gh-readonly-queue/main/pr-5")
    assert is_merge_queue_branch("gitea-mq/main")
    assert is_merge_queue_branch("staging")
    assert is_merge_queue_branch("trying")
    assert not is_merge_queue_branch("feature/foo")


def test_should_build_branch() -> None:
    branches = BranchConfigDict(
        {
            "rel": BranchConfig(
                match_glob="release-*", register_gcroots=False, update_outputs=False
            )
        }
    )
    assert should_build_branch(branches, "main", "main")
    assert should_build_branch(branches, "main", "release-1.0")
    assert should_build_branch(branches, "main", "gh-readonly-queue/main/pr-9")
    assert not should_build_branch(branches, "main", "random-feature")


def test_deduper() -> None:
    deduper = DeliveryDeduper(capacity=2)
    assert not deduper.is_duplicate("a")
    deduper.record("a")
    assert deduper.is_duplicate("a")
    deduper.record("b")
    deduper.record("c")  # evicts "a"
    assert not deduper.is_duplicate("a")
    assert deduper.is_duplicate("b")
    deduper.record("")  # missing GUID never dedupes
    assert not deduper.is_duplicate("")
    deduper.forget("b")
    assert not deduper.is_duplicate("b")
    deduper.forget("missing")  # no-op


def test_signature_validation() -> None:
    body = b'{"x": 1}'
    assert verify_github_signature(SECRET, body, sign_github(body))
    assert not verify_github_signature(SECRET, body, "sha256=deadbeef")
    assert not verify_github_signature(SECRET, body, "")


# --- parsing ------------------------------------------------------------------


def test_parse_github_push() -> None:
    event = parse_github_event(
        "push",
        {
            "ref": "refs/heads/main",
            "after": "abc123",
            "repository": {"id": 99},
            "head_commit": {"message": "fix things"},
        },
    )
    assert event == ChangeRequest(
        forge="github",
        forge_repo_id="99",
        branch="main",
        commit_sha="abc123",
        commit_message="fix things",
    )


def test_parse_github_branch_deletion_ignored() -> None:
    assert (
        parse_github_event(
            "push",
            {
                "ref": "refs/heads/gone",
                "after": "0" * 40,
                "deleted": True,
                "repository": {"id": 1},
            },
        )
        is None
    )


def test_parse_github_pr() -> None:
    payload: dict[str, Any] = {
        "action": "synchronize",
        "repository": {"id": 7},
        "pull_request": {
            "number": 12,
            "title": "Add feature",
            "user": {"login": "alice"},
            "head": {"sha": "headsha"},
            "base": {"ref": "main", "sha": "basesha"},
        },
    }
    event = parse_github_event("pull_request", payload)
    assert isinstance(event, ChangeRequest)
    assert event.pr_number == 12
    assert event.pr_author == "github:alice"
    assert event.base_sha == "basesha"
    assert event.commit_sha == "headsha"
    # PR title must not feed the [skip ci] check.
    assert event.commit_message == ""

    payload["action"] = "closed"
    closed = parse_github_event("pull_request", payload)
    assert closed == PrClosed(forge="github", forge_repo_id="7", pr_number=12)

    # Merged close must not cancel: the merge push reuses the PR build.
    payload["pull_request"]["merged"] = True
    assert parse_github_event("pull_request", payload) is None

    payload["action"] = "labeled"
    assert parse_github_event("pull_request", payload) is None


def test_parse_gitea_events() -> None:
    push = parse_gitea_event(
        "push",
        {
            "ref": "refs/heads/dev",
            "after": "ff00",
            "repository": {"id": 3},
            "head_commit": {"id": "ff00", "message": "head msg"},
            "commits": [
                {"id": "aa11", "message": "[skip ci] oldest"},
                {"id": "ff00", "message": "head msg"},
            ],
        },
    )
    assert isinstance(push, ChangeRequest)
    assert push.forge == "gitea"
    assert push.branch == "dev"
    # [skip ci] is decided on the pushed head, not commits[0].
    assert push.commit_message == "head msg"

    # Older Gitea without head_commit: fall back to the commit
    # matching `after`.
    push_no_head = parse_gitea_event(
        "push",
        {
            "ref": "refs/heads/dev",
            "after": "ff00",
            "repository": {"id": 3},
            "commits": [
                {"id": "aa11", "message": "oldest"},
                {"id": "ff00", "message": "newest"},
            ],
        },
    )
    assert isinstance(push_no_head, ChangeRequest)
    assert push_no_head.commit_message == "newest"

    pr = parse_gitea_event(
        "pull_request",
        {
            "action": "synchronized",
            "repository": {"id": 3},
            "pull_request": {
                "number": 4,
                "title": "t",
                "user": {"login": "bob"},
                "head": {"sha": "h"},
                "base": {"ref": "main", "sha": "b"},
            },
        },
    )
    assert isinstance(pr, ChangeRequest)
    assert pr.pr_author == "gitea:bob"
    # PR title must not feed the [skip ci] check.
    assert pr.commit_message == ""

    # Gitea delivers PR head updates with X-Gitea-Event pull_request_sync.
    sync = parse_gitea_event(
        "pull_request_sync",
        {
            "action": "synchronized",
            "repository": {"id": 3},
            "pull_request": {
                "number": 4,
                "user": {"login": "bob"},
                "head": {"sha": "h2"},
                "base": {"ref": "main", "sha": "b"},
            },
        },
    )
    assert isinstance(sync, ChangeRequest)
    assert sync.commit_sha == "h2"

    closed_pr: dict[str, Any] = {"number": 4, "merged": False}
    closed_payload = {
        "action": "closed",
        "repository": {"id": 3},
        "pull_request": closed_pr,
    }
    assert parse_gitea_event("pull_request", closed_payload) == PrClosed(
        forge="gitea", forge_repo_id="3", pr_number=4
    )
    closed_pr["merged"] = True
    assert parse_gitea_event("pull_request", closed_payload) is None


# --- HTTP endpoint behavior ------------------------------------------------------


@dataclass
class FakeSink:
    events: list[WebhookEvent] = field(default_factory=list)
    fail: bool = False

    async def submit(self, event: WebhookEvent) -> None:
        if self.fail:
            msg = "db down"
            raise ConnectionError(msg)
        self.events.append(event)


class FakeSecrets:
    def __init__(self, secrets: dict[str, str], *, fail: bool = False) -> None:
        self.secrets = secrets
        self.fail = fail

    async def secret_for_repo(self, forge_repo_id: str) -> str | None:
        if self.fail:
            msg = "db down"
            raise ConnectionError(msg)
        return self.secrets.get(forge_repo_id)


def make_client(
    sink: FakeSink, gitea_secrets: FakeSecrets | None = None
) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(
        create_webhook_router(
            sink,
            SECRET,
            gitea_secrets or FakeSecrets({"3": SECRET}),
            gitlab_secrets=FakeSecrets({"8": SECRET}),
        )
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


PUSH_PAYLOAD = {
    "ref": "refs/heads/main",
    "after": "abc",
    "repository": {"id": 99},
    "head_commit": {"message": "m"},
}


@pytest.mark.parametrize("path", ["/webhooks/github", "/change_hook/github"])
def test_github_endpoint_accepts_signed(path: str) -> None:
    async def run() -> None:
        sink = FakeSink()
        body = json.dumps(PUSH_PAYLOAD).encode()
        async with make_client(sink) as client:
            response = await client.post(
                path,
                content=body,
                headers={
                    "X-Hub-Signature-256": sign_github(body),
                    "X-GitHub-Event": "push",
                    "X-GitHub-Delivery": f"guid-{path}",
                },
            )
        assert response.status_code == 202
        assert len(sink.events) == 1

    asyncio.run(run())


def test_github_endpoint_rejects_bad_signature() -> None:
    async def run() -> None:
        sink = FakeSink()
        body = json.dumps(PUSH_PAYLOAD).encode()
        async with make_client(sink) as client:
            response = await client.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": "sha256=bad",
                    "X-GitHub-Event": "push",
                },
            )
        assert response.status_code == 403
        assert sink.events == []

    asyncio.run(run())


def test_github_duplicate_delivery_dropped() -> None:
    async def run() -> None:
        sink = FakeSink()
        body = json.dumps(PUSH_PAYLOAD).encode()
        headers = {
            "X-Hub-Signature-256": sign_github(body),
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "guid-dup",
        }
        async with make_client(sink) as client:
            await client.post("/webhooks/github", content=body, headers=headers)
            response = await client.post(
                "/webhooks/github", content=body, headers=headers
            )
        assert response.status_code == 202
        assert len(sink.events) == 1

    asyncio.run(run())


def test_redelivery_after_failed_submit() -> None:
    """A 500 must not record the GUID: redeliveries reuse it."""

    async def run() -> None:
        sink = FakeSink(fail=True)
        body = json.dumps(PUSH_PAYLOAD).encode()
        headers = {
            "X-Hub-Signature-256": sign_github(body),
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "guid-redelivery",
        }
        async with make_client(sink) as client:
            failed = await client.post(
                "/webhooks/github", content=body, headers=headers
            )
            assert failed.status_code == 500
            sink.fail = False
            redelivered = await client.post(
                "/webhooks/github", content=body, headers=headers
            )
        assert redelivered.status_code == 202
        assert len(sink.events) == 1

    asyncio.run(run())


def test_concurrent_duplicate_delivery() -> None:
    """The GUID is recorded before the submit, so a concurrent
    duplicate must not also reach the sink."""

    @dataclass
    class SlowSink(FakeSink):
        async def submit(self, event: WebhookEvent) -> None:
            await asyncio.sleep(0.05)
            await super().submit(event)

    async def run() -> None:
        sink = SlowSink()
        body = json.dumps(PUSH_PAYLOAD).encode()
        headers = {
            "X-Hub-Signature-256": sign_github(body),
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "guid-concurrent",
        }
        async with make_client(sink) as client:
            first, second = await asyncio.gather(
                client.post("/webhooks/github", content=body, headers=headers),
                client.post("/webhooks/github", content=body, headers=headers),
            )
        assert {first.status_code, second.status_code} == {202}
        assert len(sink.events) == 1

    asyncio.run(run())


def test_malformed_payloads_return_400() -> None:
    async def run() -> None:
        sink = FakeSink()
        body = b"not json"
        async with make_client(sink) as client:
            github = await client.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": sign_github(body),
                    "X-GitHub-Event": "push",
                },
            )
            gitea = await client.post(
                "/webhooks/gitea",
                content=body,
                headers={
                    "X-Gitea-Signature": sign_gitea(body),
                    "X-Gitea-Event": "push",
                },
            )
        assert github.status_code == 400
        assert gitea.status_code == 400
        assert sink.events == []

    asyncio.run(run())


def test_github_form_encoded_payload() -> None:
    async def run() -> None:
        sink = FakeSink()
        body = urllib.parse.urlencode({"payload": json.dumps(PUSH_PAYLOAD)}).encode()
        async with make_client(sink) as client:
            response = await client.post(
                "/webhooks/github",
                content=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Hub-Signature-256": sign_github(body),
                    "X-GitHub-Event": "push",
                    "X-GitHub-Delivery": "guid-form",
                },
            )
        assert response.status_code == 202
        assert len(sink.events) == 1

    asyncio.run(run())


def test_db_outage_returns_500() -> None:
    async def run() -> None:
        sink = FakeSink(fail=True)
        body = json.dumps(PUSH_PAYLOAD).encode()
        async with make_client(sink) as client:
            response = await client.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": sign_github(body),
                    "X-GitHub-Event": "push",
                    "X-GitHub-Delivery": "guid-outage",
                },
            )
        assert response.status_code == 500

    asyncio.run(run())


def test_gitea_endpoint_per_repo_secret() -> None:
    async def run() -> None:
        sink = FakeSink()
        payload = {
            "ref": "refs/heads/main",
            "after": "ff",
            "repository": {"id": 3},
            "commits": [{"message": "m"}],
        }
        body = json.dumps(payload).encode()
        async with make_client(sink) as client:
            ok = await client.post(
                "/webhooks/gitea",
                content=body,
                headers={
                    "X-Gitea-Signature": sign_gitea(body),
                    "X-Gitea-Event": "push",
                    "X-Gitea-Delivery": "g1",
                },
            )
            bad = await client.post(
                "/change_hook/gitea",
                content=body,
                headers={
                    "X-Gitea-Signature": "wrong",
                    "X-Gitea-Event": "push",
                },
            )
            # Unknown repo: no secret configured -> rejected.
            unknown_body = json.dumps({**payload, "repository": {"id": 777}}).encode()
            unknown = await client.post(
                "/webhooks/gitea",
                content=unknown_body,
                headers={
                    "X-Gitea-Signature": sign_gitea(unknown_body),
                    "X-Gitea-Event": "push",
                },
            )
        assert ok.status_code == 202
        assert bad.status_code == 403
        assert unknown.status_code == 403
        assert len(sink.events) == 1

    asyncio.run(run())


GITLAB_PUSH = {
    "ref": "refs/heads/main",
    "after": "abc",
    "project": {"id": 8},
    "commits": [{"id": "old", "message": "x"}, {"id": "abc", "message": "feat"}],
}

GITLAB_MR: dict[str, Any] = {
    "project": {"id": 8},
    "user": {"username": "bob"},
    "object_attributes": {
        "iid": 4,
        "action": "update",
        "oldrev": "old",
        "target_branch": "main",
        "last_commit": {"id": "def"},
    },
}


def test_parse_gitlab_events() -> None:
    push = parse_gitlab_event("Push Hook", GITLAB_PUSH)
    assert push == ChangeRequest(
        forge="gitlab",
        forge_repo_id="8",
        branch="main",
        commit_sha="abc",
        commit_message="feat",
    )
    deleted = parse_gitlab_event("Push Hook", {**GITLAB_PUSH, "after": "0" * 40})
    assert deleted is None

    mr = parse_gitlab_event("Merge Request Hook", GITLAB_MR)
    assert mr == ChangeRequest(
        forge="gitlab",
        forge_repo_id="8",
        branch="main",
        commit_sha="def",
        pr_number=4,
        pr_author="gitlab:bob",
        # No base sha in the payload: merge against the target branch.
        base_sha="refs/heads/main",
    )
    # Update without oldrev: metadata-only edit, nothing to build.
    metadata_update = parse_gitlab_event(
        "Merge Request Hook",
        {
            **GITLAB_MR,
            "object_attributes": {
                k: v for k, v in GITLAB_MR["object_attributes"].items() if k != "oldrev"
            },
        },
    )
    assert metadata_update is None

    closed = parse_gitlab_event(
        "Merge Request Hook",
        {**GITLAB_MR, "object_attributes": {"iid": 4, "action": "close"}},
    )
    assert closed == PrClosed(forge="gitlab", forge_repo_id="8", pr_number=4)
    merged = parse_gitlab_event(
        "Merge Request Hook",
        {**GITLAB_MR, "object_attributes": {"iid": 4, "action": "merge"}},
    )
    assert merged is None


def test_gitlab_endpoint_token_validation() -> None:
    async def run() -> None:
        sink = FakeSink()
        async with make_client(sink) as client:
            headers = {
                "X-Gitlab-Event": "Push Hook",
                "X-Gitlab-Event-UUID": "u1",
                "Content-Type": "application/json",
            }
            body = json.dumps(GITLAB_PUSH).encode()
            bad = await client.post(
                "/webhooks/gitlab",
                content=body,
                headers={**headers, "X-Gitlab-Token": "wrong"},
            )
            assert bad.status_code == 403
            ok = await client.post(
                "/webhooks/gitlab",
                content=body,
                headers={**headers, "X-Gitlab-Token": SECRET},
            )
            assert ok.status_code == 202
            dup = await client.post(
                "/webhooks/gitlab",
                content=body,
                headers={**headers, "X-Gitlab-Token": SECRET},
            )
            assert dup.status_code == 202
            assert dup.text == "duplicate delivery"
        assert len(sink.events) == 1
        assert sink.events[0].forge == "gitlab"

    asyncio.run(run())
