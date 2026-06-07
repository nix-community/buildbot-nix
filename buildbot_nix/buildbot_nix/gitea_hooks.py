"""Gitea webhook auto-registration with per-repo secrets.

When a project is enabled, the engine generates a per-repository
secret, stores it (gitea_webhook_secrets, read by webhook validation),
and registers a webhook pointing at `<webhook_base_url>/webhooks/gitea`.
The webhook base URL may differ from the UI URL (`webhookBaseUrl`).

Leftover buildbot-era webhooks are removed only when their URL matches
this instance's own configured webhook base URL — never "anything that
looks like buildbot".
"""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING, Any

from .forge import ForgeError

if TYPE_CHECKING:
    import asyncpg

    from .forge import GiteaClient

logger = logging.getLogger(__name__)

LEGACY_HOOK_PATH = "/change_hook/gitea"
HOOK_PATH = "/webhooks/gitea"


class GiteaWebhookSecrets:
    """Implements the webhook router's GiteaSecretStore protocol."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def secret_for_repo(self, forge_repo_id: str) -> str | None:
        return await self.pool.fetchval(
            """
            SELECT s.secret FROM gitea_webhook_secrets s
            JOIN projects p ON p.id = s.project_id
            WHERE p.forge = 'gitea' AND p.forge_repo_id = $1
            """,
            forge_repo_id,
        )

    async def get_or_create(self, project_id: int) -> str:
        secret = await self.pool.fetchval(
            "SELECT secret FROM gitea_webhook_secrets WHERE project_id = $1",
            project_id,
        )
        if secret is not None:
            return secret
        secret = secrets.token_hex(32)
        # Concurrent creation: first writer wins.
        return await self.pool.fetchval(
            """
            INSERT INTO gitea_webhook_secrets (project_id, secret)
            VALUES ($1, $2)
            ON CONFLICT (project_id) DO UPDATE SET secret = gitea_webhook_secrets.secret
            RETURNING secret
            """,
            project_id,
            secret,
        )

    async def rotate(self, project_id: int) -> str:
        """Replace the secret; the old one stops verifying immediately.
        Auto-managed hooks re-sync on the next discovery cycle."""
        secret = secrets.token_hex(32)
        return await self.pool.fetchval(
            """
            INSERT INTO gitea_webhook_secrets (project_id, secret)
            VALUES ($1, $2)
            ON CONFLICT (project_id) DO UPDATE SET secret = EXCLUDED.secret
            RETURNING secret
            """,
            project_id,
            secret,
        )


def hook_url(webhook_base_url: str) -> str:
    return webhook_base_url.rstrip("/") + HOOK_PATH


def legacy_hook_urls(webhook_base_url: str) -> set[str]:
    base = webhook_base_url.rstrip("/")
    # Old buildbot deployments registered with or without trailing slash.
    return {base + LEGACY_HOOK_PATH, base + LEGACY_HOOK_PATH + "/"}


async def register_repo_hook(  # noqa: PLR0913
    client: GiteaClient,
    secrets_store: GiteaWebhookSecrets,
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
