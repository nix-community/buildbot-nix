"""Gitea webhook auto-registration.

When a project is enabled, the service stores a per-repository secret
(hook_secrets.py) and registers a webhook pointing at
`<webhook_base_url>/webhooks/gitea`.
The webhook base URL may differ from the UI URL (`webhookBaseUrl`).

Leftover buildbot-era webhooks are removed only when their URL matches
this instance's own configured webhook base URL — never "anything that
looks like buildbot".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .forge import ForgeError

if TYPE_CHECKING:
    from .forge import GiteaClient
    from .hook_secrets import WebhookSecrets

logger = logging.getLogger(__name__)

LEGACY_HOOK_PATH = "/change_hook/gitea"
HOOK_PATH = "/webhooks/gitea"


def hook_url(webhook_base_url: str) -> str:
    return webhook_base_url.rstrip("/") + HOOK_PATH


def legacy_hook_urls(webhook_base_url: str) -> set[str]:
    base = webhook_base_url.rstrip("/")
    # Old buildbot deployments registered with or without trailing slash.
    return {base + LEGACY_HOOK_PATH, base + LEGACY_HOOK_PATH + "/"}


async def register_repo_hook(  # noqa: PLR0913
    client: GiteaClient,
    secrets_store: WebhookSecrets,
    project_id: int,
    owner: str,
    repo: str,
    webhook_base_url: str,
) -> None:
    """Idempotently register our webhook; remove legacy buildbot hooks
    only when they point at our own webhook base URL."""
    secret = await secrets_store.get_or_create(project_id)
    target_url = hook_url(webhook_base_url)
    legacy_urls = legacy_hook_urls(webhook_base_url)

    try:
        hooks: list[dict[str, Any]] = await client._paginated(  # noqa: SLF001
            f"{client.instance_url}/api/v1/repos/{owner}/{repo}/hooks?limit=100"
        )
    except ForgeError as e:
        if "403" not in str(e):
            raise
        # Hook management needs repo-admin permission; the project still
        # works if the webhook is created manually.
        logger.warning(
            "no admin permission to manage webhooks; create one manually "
            "for push, pull_request and pull_request_sync events",
            extra={"repo": f"{owner}/{repo}", "url": target_url},
        )
        return
    existing_id: int | None = None
    for hook in hooks:
        url = (hook.get("config") or {}).get("url", "")
        if url == target_url:
            existing_id = hook["id"]
        elif url in legacy_urls:
            logger.info(
                "removing legacy buildbot webhook",
                extra={"repo": f"{owner}/{repo}", "url": url},
            )
            await client.http.delete(
                f"{client.instance_url}/api/v1/repos/{owner}/{repo}/hooks/{hook['id']}",
                headers=client._headers(),  # noqa: SLF001
            )

    hook_body = {
        "name": "web",
        "active": True,
        # "pull_request" alone does not cover pushes to an open
        # PR; Gitea delivers those as "pull_request_sync".
        "events": ["push", "pull_request", "pull_request_sync"],
        "type": "gitea",
        "config": {
            "url": target_url,
            "content_type": "json",
            "secret": secret,
        },
    }
    if existing_id is not None:
        # The existing hook may carry a stale secret (e.g. after a
        # database reset) and Gitea never exposes it, so re-sync in place.
        response = await client.http.patch(
            f"{client.instance_url}/api/v1/repos/{owner}/{repo}/hooks/{existing_id}",
            headers={**client._headers(), "Content-Type": "application/json"},  # noqa: SLF001
            json=hook_body,
        )
        if response.status_code >= 400:  # noqa: PLR2004
            logger.error(
                "failed to update webhook",
                extra={"repo": f"{owner}/{repo}", "status": response.status_code},
            )
        return
    logger.info("registering webhook", extra={"repo": f"{owner}/{repo}"})
    response = await client.http.post(
        f"{client.instance_url}/api/v1/repos/{owner}/{repo}/hooks",
        headers={**client._headers(), "Content-Type": "application/json"},  # noqa: SLF001
        json=hook_body,
    )
    if response.status_code >= 400:  # noqa: PLR2004
        logger.error(
            "failed to register webhook",
            extra={"repo": f"{owner}/{repo}", "status": response.status_code},
        )
