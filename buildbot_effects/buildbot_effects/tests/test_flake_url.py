"""Test _flake_url: the bridge between EffectsOptions and builtins.getFlake."""

from __future__ import annotations

from pathlib import Path

from buildbot_effects import _flake_url
from buildbot_effects.options import EffectsOptions


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
