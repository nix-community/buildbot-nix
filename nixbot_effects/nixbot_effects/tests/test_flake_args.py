"""How EffectsOptions turn into flake arguments: the _flake_url
bridge to builtins.getFlake, and rev resolution in effects_args()
(--branch without --rev must use the branch tip, not the checkout's
HEAD; https://github.com/nix-community/nixbot/issues/583)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from nixbot_effects import _flake_url, effects_args, git_get_tag, secret_context
from nixbot_effects.options import EffectsOptions
from nixbot_effects.secrets import SimpleSecret, gather_secrets


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def test_branch_resolves_to_branch_tip(tmp_path: Path) -> None:
    """--branch should resolve rev to the tip of that branch, not HEAD."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@test")

    # Initial commit on main
    (repo / "file.txt").write_text("main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    main_rev = _git(repo, "rev-parse", "HEAD")

    # Create a feature branch with a new commit
    _git(repo, "checkout", "-b", "feature")
    (repo / "file.txt").write_text("feature")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feature commit")
    feature_rev = _git(repo, "rev-parse", "HEAD")

    # Go back to main — HEAD is now main_rev
    _git(repo, "checkout", "main")
    assert _git(repo, "rev-parse", "HEAD") == main_rev

    # Ask for --branch=feature without --rev: should get feature_rev, not main_rev
    opts = EffectsOptions(path=repo, branch="feature")
    result = effects_args(opts)

    assert result["rev"] == feature_rev, (
        f"Expected rev from 'feature' branch ({feature_rev[:7]}), "
        f"got HEAD ({result['rev'][:7]})"
    )
    assert result["branch"] == "feature"


def test_git_tag_propagates_to_secret_context(tmp_path: Path) -> None:
    """A tag resolved from git must end up in opts.tag so that
    isTag-conditioned secrets can be granted."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@test")
    (repo / "file.txt").write_text("v1")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "release")
    _git(repo, "tag", "v1.0")

    opts = EffectsOptions(path=repo, repo="acme/widget")
    result = effects_args(opts)
    assert result["tag"] == "v1.0"
    assert opts.tag == "v1.0"

    ctx = secret_context(opts)
    assert ctx.ref == "refs/tags/v1.0"
    out = gather_secrets(
        {"deploy": SimpleSecret("release-key")},
        {"release-key": {"data": {"token": "s3cret"}, "condition": "isTag"}},
        ctx,
        None,
    )
    assert out == {"deploy": {"data": {"token": "s3cret"}}}


class TestFlakeUrl:
    def test_local_path_fallback(self) -> None:
        opts = EffectsOptions(path=Path("/home/user/my-repo"))
        assert (
            _flake_url(opts, "abc1234") == "git+file:///home/user/my-repo?rev=abc1234#"
        )

    def test_locked_url_used(self) -> None:
        opts = EffectsOptions(
            path=Path("/nix/store/xyz-source"),
            locked_url="github:org/repo/abc1234def5678",
        )
        assert _flake_url(opts, "abc1234") == "github:org/repo/abc1234def5678"

    def test_empty_locked_url_falls_back(self) -> None:
        opts = EffectsOptions(path=Path("/home/user/my-repo"), locked_url="")
        assert (
            _flake_url(opts, "abc1234") == "git+file:///home/user/my-repo?rev=abc1234#"
        )


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@test")
    (repo / "file.txt").write_text("content")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_git_get_tag_single_tag(tmp_path: Path) -> None:
    """A commit with exactly one tag must return that tag."""
    repo, rev = _init_repo(tmp_path)
    _git(repo, "tag", "v1.0.0")

    assert git_get_tag(repo, rev) == "v1.0.0"


def test_git_get_tag_no_tag(tmp_path: Path) -> None:
    repo, rev = _init_repo(tmp_path)

    assert git_get_tag(repo, rev) is None
