"""Webhook ingestion.

Endpoints `/webhooks/{github,gitea}` plus legacy buildbot aliases
`/change_hook/{github,gitea}` (identical validation). GitHub payloads
are verified against the App-level webhook secret
(X-Hub-Signature-256); Gitea payloads against the per-repository
secret stored in the database (X-Gitea-Signature). Deliveries are
deduplicated by delivery GUID. A database outage makes handlers fail
fast with 500 so the GitHub App redelivers (Gitea is backstopped by
startup reconciliation).

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


def verify_github_signature(secret: str, body: bytes, signature_header: str) -> bool:
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header.removeprefix("sha256="), expected)


def verify_gitea_signature(secret: str, body: bytes, signature_header: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header, expected)


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


def parse_github_event(  # noqa: PLR0911
    event_type: str, payload: dict[str, Any]
) -> WebhookEvent | None:
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
        action = payload.get("action", "")
        pr = payload.get("pull_request") or {}
        number = pr.get("number")
        if number is None:
            return None
        if action == "closed":
            if pr.get("merged"):
                # No cancel on merge: the merge push reuses the PR
                # build (same post-merge tree hash).
                return None
            return PrClosed(forge="github", forge_repo_id=repo_id, pr_number=number)
        if action not in ("opened", "synchronize", "reopened"):
            return None
        # No commit_message: the [skip ci] check must not run on the
        # PR title, and the payload lacks the head commit message.
        return ChangeRequest(
            forge="github",
            forge_repo_id=repo_id,
            branch=(pr.get("base") or {}).get("ref", ""),
            commit_sha=(pr.get("head") or {}).get("sha", ""),
            pr_number=number,
            pr_author=f"github:{(pr.get('user') or {}).get('login', '')}",
            base_sha=(pr.get("base") or {}).get("sha"),
        )
    return None


def parse_gitea_event(  # noqa: PLR0911
    event_type: str, payload: dict[str, Any]
) -> WebhookEvent | None:
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
        action = payload.get("action", "")
        pr = payload.get("pull_request") or {}
        number = pr.get("number")
        if number is None:
            return None
        if action == "closed":
            if pr.get("merged"):
                # See GitHub case: no cancel on merge.
                return None
            return PrClosed(forge="gitea", forge_repo_id=repo_id, pr_number=number)
        if action not in ("opened", "synchronized", "reopened"):
            return None
        # No commit_message; see parse_github_event.
        return ChangeRequest(
            forge="gitea",
            forge_repo_id=repo_id,
            branch=(pr.get("base") or {}).get("ref", ""),
            commit_sha=(pr.get("head") or {}).get("sha", ""),
            pr_number=number,
            pr_author=f"gitea:{(pr.get('user') or {}).get('login', '')}",
            base_sha=(pr.get("base") or {}).get("sha"),
        )
    return None


# --- FastAPI wiring -----------------------------------------------------------------


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


class ChangeSink(Protocol):
    """Receives parsed webhook events; the orchestrator side implements
    this. Must raise on database outage (translated to 500)."""

    async def submit(self, event: WebhookEvent) -> None: ...


class GiteaSecretStore(Protocol):
    async def secret_for_repo(self, forge_repo_id: str) -> str | None: ...


class _WebhookHandlers:
    def __init__(
        self,
        sink: ChangeSink,
        github_webhook_secret: str | None,
        gitea_secrets: GiteaSecretStore | None,
        deduper: DeliveryDeduper,
    ) -> None:
        self.sink = sink
        self.github_webhook_secret = github_webhook_secret
        self.gitea_secrets = gitea_secrets
        self.deduper = deduper

    async def handle_github(self, request: Request) -> Response:
        if self.github_webhook_secret is None:
            raise HTTPException(status_code=404, detail="github not configured")
        body = await request.body()
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
        body = await request.body()
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
    gitea_secrets: GiteaSecretStore | None,
    deduper: DeliveryDeduper | None = None,
) -> APIRouter:
    router = APIRouter()
    handlers = _WebhookHandlers(
        sink, github_webhook_secret, gitea_secrets, deduper or DeliveryDeduper()
    )
    # New paths plus legacy buildbot aliases with identical validation.
    for path in ("/webhooks/github", "/change_hook/github"):
        router.post(path)(handlers.handle_github)
    for path in ("/webhooks/gitea", "/change_hook/gitea"):
        router.post(path)(handlers.handle_gitea)
    return router
