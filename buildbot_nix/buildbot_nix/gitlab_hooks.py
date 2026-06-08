"""GitLab webhook auto-registration.

Same flow as gitea_hooks.py: the service stores a per-repository secret
(hook_secrets.py) and registers a webhook pointing at
`<webhook_base_url>/webhooks/gitlab`. Hook management needs Maintainer
on the project; without it the hook must be created manually.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .forge import ForgeError

if TYPE_CHECKING:
    from .forge import GitlabClient
    from .hook_secrets import WebhookSecrets

logger = logging.getLogger(__name__)

HOOK_PATH = "/webhooks/gitlab"


def hook_url(webhook_base_url: str) -> str:
    return webhook_base_url.rstrip("/") + HOOK_PATH


async def register_repo_hook(  # noqa: PLR0913
    client: GitlabClient,
    secrets_store: WebhookSecrets,
    project_id: int,
    owner: str,
    repo: str,
    webhook_base_url: str,
) -> None:
    """Idempotently register our webhook."""
    secret = await secrets_store.get_or_create(project_id)
    target_url = hook_url(webhook_base_url)
    api = client.project_api_url(owner, repo)

    try:
        hooks: list[dict[str, Any]] = await client.paginated(f"{api}/hooks")
    except ForgeError as e:
        if "403" not in str(e):
            raise
        logger.warning(
            "no maintainer permission to manage webhooks; create one "
            "manually for push and merge request events",
            extra={"repo": f"{owner}/{repo}", "url": target_url},
        )
        return
    existing_id = next(
        (hook["id"] for hook in hooks if hook.get("url") == target_url), None
    )

    hook_body = {
        "url": target_url,
        "token": secret,
        "push_events": True,
        "merge_requests_events": True,
        "enable_ssl_verification": True,
    }
    if existing_id is not None:
        # The existing hook may carry a stale secret (e.g. after a
        # database reset) and GitLab never exposes it, so re-sync in place.
        response = await client.http.put(
            f"{api}/hooks/{existing_id}",
            headers=client.auth_headers(),
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
        f"{api}/hooks",
        headers=client.auth_headers(),
        json=hook_body,
    )
    if response.status_code >= 400:  # noqa: PLR2004
        logger.error(
            "failed to register webhook",
            extra={"repo": f"{owner}/{repo}", "status": response.status_code},
        )
