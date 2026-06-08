"""Branch config lookup with multiple matching globs.

A branch may match several configured globs (e.g. "*" and
"release-*"); merging their settings must not raise just because the
globs differ -- otherwise every webhook for such a branch 500s.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nixbot.config import (
    BranchConfig,
    BranchConfigDict,
    ConfigError,
    GiteaConfig,
    GitHubConfig,
)


def _bc(glob: str, *, gcroots: bool, outputs: bool) -> BranchConfig:
    return BranchConfig(
        match_glob=glob,
        register_gcroots=gcroots,
        update_outputs=outputs,
    )


def test_lookup_overlapping_globs_merges_settings() -> None:
    cfg = BranchConfigDict(
        {
            "all": _bc("*", gcroots=False, outputs=True),
            "release": _bc("release-*", gcroots=True, outputs=False),
        }
    )
    merged = cfg.lookup_branch_config("release-1")
    assert merged is not None
    assert merged.register_gcroots is True
    assert merged.update_outputs is True


def test_lookup_single_match() -> None:
    cfg = BranchConfigDict(
        {
            "release": _bc("release-*", gcroots=True, outputs=False),
        }
    )
    merged = cfg.lookup_branch_config("release-1")
    assert merged is not None
    assert merged.register_gcroots is True
    assert merged.update_outputs is False
    assert cfg.lookup_branch_config("main") is None


def test_oauth_secret_without_file_has_message() -> None:
    """Missing oauth_secret_file must fail with a descriptive error."""
    gitea = GiteaConfig(instance_url="https://gitea.example.com", oauth_id="x")
    with pytest.raises(ConfigError, match=r"gitea\.oauth_secret_file"):
        _ = gitea.oauth_secret

    github = GitHubConfig(id=1, secret_key_file=Path("key"), oauth_id="x")
    with pytest.raises(ConfigError, match=r"github\.oauth_secret_file"):
        _ = github.oauth_secret
