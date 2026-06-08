"""Branch config lookup with multiple matching globs.

A branch may match several configured globs (e.g. "*" and
"release-*"); merging their settings must not raise just because the
globs differ -- otherwise every webhook for such a branch 500s.
"""

from __future__ import annotations

from buildbot_nix.config import BranchConfig, BranchConfigDict


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
