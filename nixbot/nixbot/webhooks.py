"""Webhook ingestion.

Endpoints `/webhooks/{github,gitea,gitlab}` plus the legacy buildbot
alias `/change_hook/github` (identical validation; the GitHub App
webhook secret is deployment-wide, so legacy hooks still verify).
GitHub
payloads are verified against the App-level webhook secret
(X-Hub-Signature-256); Gitea and GitLab payloads against the
per-repository secret stored in the database (HMAC X-Gitea-Signature
vs. plain-token X-Gitlab-Token - GitLab does not sign). Deliveries are
deduplicated by delivery GUID. A database outage makes handlers fail
fast with 500 so the GitHub App redelivers (Gitea and GitLab are
backstopped by startup reconciliation).

All pull requests build (no trust gating — the Nix sandbox is the
trust boundary); merge-queue branches always build.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, HTTPException, Request, Response

if TYPE_CHECKING:
    from .config import BranchConfigDict

logger = logging.getLogger(__name__)

# Unauthenticated endpoints must not buffer unbounded request bodies;
# GitHub itself caps webhook payloads at 25 MB.
MAX_BODY_SIZE = 25 * 1024 * 1024

MERGE_QUEUE_PATTERNS = ("gh-readonly-queue/*", "gitea-mq/*", "staging", "trying")


def is_merge_queue_branch(branch: str) -> bool:
    return any(fnmatch(branch, pattern) for pattern in MERGE_QUEUE_PATTERNS)


def should_build_branch(
    branches: BranchConfigDict, default_branch: str, branch: str
) -> bool:
    """Default branch, configured extra branches, and merge-queue
    branches build; everything else is ignored (PRs always build and
    are decided separately)."""
    return is_merge_queue_branch(branch) or branches.do_run(default_branch, branch)


# --- signature validation -------------------------------------------------------


def _constant_time_eq(a: str, b: str) -> bool:
    """hmac.compare_digest with str args raises TypeError on non-ASCII
    input; forge headers are attacker-controlled, so compare bytes."""
    return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode())


def verify_github_signature(secret: str, body: bytes, signature_header: str) -> bool:
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return _constant_time_eq(signature_header.removeprefix("sha256="), expected)


def verify_gitea_signature(secret: str, body: bytes, signature_header: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return _constant_time_eq(signature_header, expected)


class DeliveryDeduper:
    """LRU set of recently seen delivery GUIDs.

    A GUID is recorded before its event is submitted (blocking
    concurrent duplicates) and forgotten when the submit fails, so
    forge redeliveries (same GUID) are accepted.
    """

    def __init__(self, capacity: int = 10000) -> None:
        self.capacity = capacity
        self._seen: OrderedDict[str, None] = OrderedDict()

    def is_duplicate(self, guid: str) -> bool:
        if not guid:
            return False
        if guid in self._seen:
            self._seen.move_to_end(guid)
            return True
        return False

    def record(self, guid: str) -> None:
        if not guid:
            return
        self._seen[guid] = None
        if len(self._seen) > self.capacity:
            self._seen.popitem(last=False)

    def forget(self, guid: str) -> None:
        self._seen.pop(guid, None)


# --- payload parsing --------------------------------------------------------------


@dataclass(frozen=True)
class ChangeRequest:
    forge: str
    forge_repo_id: str
    branch: str
    commit_sha: str
    commit_message: str = ""
    pr_number: int | None = None
    pr_author: str | None = None
    base_sha: str | None = None


@dataclass(frozen=True)
class PrClosed:
    forge: str
    forge_repo_id: str
    pr_number: int


WebhookEvent = ChangeRequest | PrClosed


def _pr_action_builds(action: str, payload: dict[str, Any], sync_action: str) -> bool:
    """ "edited" only matters when the base branch changed (retarget):
    the existing status is green against the old base. Title/body edits
    carry neither key and stay ignored. GitHub reports the old base as
    changes.base, Gitea as changes.ref (PullRequestChangeTargetBranch)."""
    if action == "edited":
        changes = payload.get("changes") or {}
        return bool(changes.get("base") or changes.get("ref"))
    return action in ("opened", sync_action, "reopened")


def _parse_pr_event(
    forge: str, repo_id: str, payload: dict[str, Any], sync_action: str
) -> WebhookEvent | None:
    """GitHub/Gitea pull_request payloads share this shape."""
    pr = payload.get("pull_request") or {}
    number = pr.get("number")
    if number is None:
        return None
    action = payload.get("action", "")
    if action == "closed":
        if pr.get("merged"):
            # No cancel on merge: the merge push reuses the PR build
            # (same post-merge tree hash).
            return None
        return PrClosed(forge=forge, forge_repo_id=repo_id, pr_number=number)
    if not _pr_action_builds(action, payload, sync_action):
        return None
    # No commit_message: the [skip ci] check must not run on the PR
    # title, and the payload lacks the head commit message.
    base = pr.get("base") or {}
    base_ref = base.get("ref", "")
    return ChangeRequest(
        forge=forge,
        forge_repo_id=repo_id,
        branch=base_ref,
        commit_sha=(pr.get("head") or {}).get("sha", ""),
        pr_number=number,
        pr_author=f"{forge}:{(pr.get('user') or {}).get('login', '')}",
        # base.sha is frozen at PR creation while the base branch moves
        # on; merge into the branch tip (fetched alongside) so the PR is
        # tested against the current base.
        base_sha=f"refs/heads/{base_ref}" if base_ref else base.get("sha"),
    )


def parse_github_event(event_type: str, payload: dict[str, Any]) -> WebhookEvent | None:
    repo = payload.get("repository") or {}
    repo_id = str(repo.get("id", ""))
    if not repo_id:
        return None
    if event_type == "push":
        ref = payload.get("ref", "")
        if not ref.startswith("refs/heads/") or payload.get("deleted"):
            return None
        head = payload.get("after", "")
        if not head or set(head) == {"0"}:
            return None
        head_commit = payload.get("head_commit") or {}
        return ChangeRequest(
            forge="github",
            forge_repo_id=repo_id,
            branch=ref.removeprefix("refs/heads/"),
            commit_sha=head,
            commit_message=head_commit.get("message", ""),
        )
    if event_type == "pull_request":
        return _parse_pr_event("github", repo_id, payload, "synchronize")
    return None


def parse_gitea_event(event_type: str, payload: dict[str, Any]) -> WebhookEvent | None:
    repo = payload.get("repository") or {}
    repo_id = str(repo.get("id", ""))
    if not repo_id:
        return None
    if event_type == "push":
        ref = payload.get("ref", "")
        if not ref.startswith("refs/heads/"):
            return None
        head = payload.get("after", "")
        if not head or set(head) == {"0"}:
            return None
        # Gitea lists `commits` oldest-first; the pushed head is
        # `head_commit` (fall back to the commit matching `after`).
        head_commit: dict[str, Any] = payload.get("head_commit") or next(
            (
                commit
                for commit in payload.get("commits") or []
                if (commit or {}).get("id") == head
            ),
            {},
        )
        return ChangeRequest(
            forge="gitea",
            forge_repo_id=repo_id,
            branch=ref.removeprefix("refs/heads/"),
            commit_sha=head,
            commit_message=head_commit.get("message", ""),
        )
    # Gitea delivers PR head updates as a separate "pull_request_sync"
    # hook event (action "synchronized").
    if event_type in ("pull_request", "pull_request_sync"):
        return _parse_pr_event("gitea", repo_id, payload, "synchronized")
    return None


# --- FastAPI wiring -----------------------------------------------------------------


def parse_gitlab_event(  # noqa: PLR0911
    event_type: str, payload: dict[str, Any]
) -> WebhookEvent | None:
    repo_id = str((payload.get("project") or {}).get("id", ""))
    if not repo_id:
        return None
    if event_type == "Push Hook":
        ref = payload.get("ref", "")
        if not ref.startswith("refs/heads/"):
            return None
        head = payload.get("after", "")
        if not head or set(head) == {"0"}:
            return None
        head_commit: dict[str, Any] = next(
            (
                commit
                for commit in payload.get("commits") or []
                if (commit or {}).get("id") == head
            ),
            {},
        )
        return ChangeRequest(
            forge="gitlab",
            forge_repo_id=repo_id,
            branch=ref.removeprefix("refs/heads/"),
            commit_sha=head,
            commit_message=head_commit.get("message", ""),
        )
    if event_type == "Merge Request Hook":
        attrs = payload.get("object_attributes") or {}
        number = attrs.get("iid")
        if number is None:
            return None
        action = attrs.get("action", "")
        if action == "close":
            return PrClosed(forge="gitlab", forge_repo_id=repo_id, pr_number=number)
        # No cancel on merge; see parse_github_event.
        if action not in ("open", "update", "reopen"):
            return None
        # Metadata-only updates (labels, title, milestone) carry no
        # oldrev; only head-moving updates trigger a build.
        if action == "update" and not attrs.get("oldrev"):
            return None
        # No commit_message; see parse_github_event.
        target_branch = attrs.get("target_branch", "")
        # payload["user"] is the event actor, not the MR author (only
        # the author's numeric id is in the payload). pr_author grants
        # restart/cancel rights, so attribute the actor only where they
        # own the change: "open" (author) and head-moving "update"
        # (pusher), never "reopen" by someone else.
        actor = (payload.get("user") or {}).get("username", "")
        pr_author = (
            f"gitlab:{actor}" if actor and action in ("open", "update") else None
        )
        return ChangeRequest(
            forge="gitlab",
            forge_repo_id=repo_id,
            branch=target_branch,
            commit_sha=(attrs.get("last_commit") or {}).get("id", ""),
            pr_number=number,
            pr_author=pr_author,
            # The payload has no base sha; the target branch head was
            # fetched alongside, so merge against its ref.
            base_sha=f"refs/heads/{target_branch}" if target_branch else None,
        )
    return None


def parse_webhook_body(request: Request, body: bytes) -> dict[str, Any]:
    """Decode the webhook payload; malformed input is a client error
    (400), never a 500 that would trigger pointless redeliveries."""
    content_type = request.headers.get("Content-Type", "")
    try:
        if content_type.startswith("application/x-www-form-urlencoded"):
            # GitHub hooks configured with form content type wrap
            # the JSON document in a `payload` form field.
            fields = urllib.parse.parse_qs(body.decode())
            payload = json.loads(fields["payload"][0])
        else:
            payload = json.loads(body)
    except (KeyError, ValueError, UnicodeDecodeError) as e:
        raise HTTPException(status_code=400, detail="malformed payload") from e
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="malformed payload")
    return payload


async def read_body(request: Request) -> bytes:
    """Read the request body, rejecting oversized payloads with 413
    before (Content-Length) or while (chunked) buffering."""
    length = request.headers.get("Content-Length", "")
    if length.isdigit() and int(length) > MAX_BODY_SIZE:
        raise HTTPException(status_code=413, detail="payload too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_SIZE:
            raise HTTPException(status_code=413, detail="payload too large")
    return bytes(body)


class ChangeSink(Protocol):
    """Receives parsed webhook events; the orchestrator side implements
    this. Must raise on database outage (translated to 500)."""

    async def submit(self, event: WebhookEvent) -> None: ...


class SecretStore(Protocol):
    async def secret_for_repo(self, forge_repo_id: str) -> str | None: ...


class _WebhookHandlers:
    def __init__(
        self,
        sink: ChangeSink,
        github_webhook_secret: str | None,
        gitea_secrets: SecretStore | None,
        gitlab_secrets: SecretStore | None,
        deduper: DeliveryDeduper,
    ) -> None:
        self.sink = sink
        self.github_webhook_secret = github_webhook_secret
        self.gitea_secrets = gitea_secrets
        self.gitlab_secrets = gitlab_secrets
        self.deduper = deduper

    async def handle_github(self, request: Request) -> Response:
        if self.github_webhook_secret is None:
            raise HTTPException(status_code=404, detail="github not configured")
        body = await read_body(request)
        if not verify_github_signature(
            self.github_webhook_secret,
            body,
            request.headers.get("X-Hub-Signature-256", ""),
        ):
            raise HTTPException(status_code=403, detail="invalid signature")
        guid = request.headers.get("X-GitHub-Delivery", "")
        if self.deduper.is_duplicate(guid):
            return Response(status_code=202, content="duplicate delivery")
        event = parse_github_event(
            request.headers.get("X-GitHub-Event", ""), parse_webhook_body(request, body)
        )
        return await self._dispatch(guid, event)

    async def handle_gitea(self, request: Request) -> Response:
        if self.gitea_secrets is None:
            raise HTTPException(status_code=404, detail="gitea not configured")
        body = await read_body(request)
        payload = parse_webhook_body(request, body)
        repo_id = str((payload.get("repository") or {}).get("id", ""))
        try:
            secret = await self.gitea_secrets.secret_for_repo(repo_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail="database unavailable") from e
        if secret is None or not verify_gitea_signature(
            secret, body, request.headers.get("X-Gitea-Signature", "")
        ):
            raise HTTPException(status_code=403, detail="invalid signature")
        guid = request.headers.get("X-Gitea-Delivery", "")
        if self.deduper.is_duplicate(guid):
            return Response(status_code=202, content="duplicate delivery")
        event = parse_gitea_event(request.headers.get("X-Gitea-Event", ""), payload)
        return await self._dispatch(guid, event)

    async def handle_gitlab(self, request: Request) -> Response:
        if self.gitlab_secrets is None:
            raise HTTPException(status_code=404, detail="gitlab not configured")
        body = await read_body(request)
        payload = parse_webhook_body(request, body)
        repo_id = str((payload.get("project") or {}).get("id", ""))
        try:
            secret = await self.gitlab_secrets.secret_for_repo(repo_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail="database unavailable") from e
        token = request.headers.get("X-Gitlab-Token", "")
        if secret is None or not _constant_time_eq(token, secret):
            raise HTTPException(status_code=403, detail="invalid token")
        guid = request.headers.get("X-Gitlab-Event-UUID", "")
        if self.deduper.is_duplicate(guid):
            return Response(status_code=202, content="duplicate delivery")
        event = parse_gitlab_event(request.headers.get("X-Gitlab-Event", ""), payload)
        return await self._dispatch(guid, event)

    async def _dispatch(self, guid: str, event: WebhookEvent | None) -> Response:
        if event is None:
            self.deduper.record(guid)
            return Response(status_code=200, content="ignored")
        await self._submit(event, guid)
        return Response(status_code=202, content="accepted")

    async def _submit(self, event: WebhookEvent, guid: str) -> None:
        self.deduper.record(guid)
        try:
            await self.sink.submit(event)
        except Exception as e:
            self.deduper.forget(guid)
            # Fail fast on DB outage: the GitHub App redelivers.
            logger.exception("failed to submit change event")
            raise HTTPException(
                status_code=500, detail="temporarily unavailable"
            ) from e


def create_webhook_router(
    sink: ChangeSink,
    github_webhook_secret: str | None,
    gitea_secrets: SecretStore | None,
    gitlab_secrets: SecretStore | None = None,
    deduper: DeliveryDeduper | None = None,
) -> APIRouter:
    router = APIRouter()
    handlers = _WebhookHandlers(
        sink,
        github_webhook_secret,
        gitea_secrets,
        gitlab_secrets,
        deduper or DeliveryDeduper(),
    )
    # No legacy gitea alias: old buildbot hooks carry secrets that can
    # never match the per-repo secrets this service generates, and the
    # reconciler replaces those hooks anyway.
    for path in ("/webhooks/github", "/change_hook/github"):
        router.post(path)(handlers.handle_github)
    router.post("/webhooks/gitea")(handlers.handle_gitea)
    router.post("/webhooks/gitlab")(handlers.handle_gitlab)
    return router
