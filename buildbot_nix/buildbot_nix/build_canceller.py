"""Build canceller configuration for buildbot-nix."""

from typing import TYPE_CHECKING, Any

from buildbot.plugins import util

if TYPE_CHECKING:
    from .projects import GitProject


def branch_key_for_pr(build: dict[str, Any]) -> str:
    """Extract a unique key for PR/change to cancel old builds."""
    branch = build.get("branch") or ""

    # For GitHub/Gitea/GitLab PRs
    if branch.startswith(("refs/pull/", "refs/merge-requests/")):
        # refs/pull/123/merge -> refs/pull/123
        # refs/merge-requests/123/head -> refs/merge-requests/123
        return branch.rsplit("/", 1)[0]

    # For regular branches
    return branch


def create_build_canceller(projects: list["GitProject"]) -> util.OldBuildCanceller:
    """Create OldBuildCanceller service for the given projects.

    Args:
        projects: List of GitProject instances to configure cancellation for.

    Returns:
        Configured OldBuildCanceller service.
    """

    return util.OldBuildCanceller(
        "build_canceller",
        filters=[
            (
                [
                    f"{project.name}/nix-eval"
                ],  # Cancel eval builds, this also cancels build itself.
                util.SourceStampFilter(project_eq=[project.name]),
            )
            for project in projects
        ],
        branch_key=branch_key_for_pr,
    )
