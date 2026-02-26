"""Tests for CLI flake ref support."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from buildbot_effects.cli import run_command

FAKE_METADATA: dict = {
    "url": "github:my-org/my-repo/main",
    "resolvedUrl": "github:my-org/my-repo/abc1234",
    "lockedUrl": "github:my-org/my-repo/abc1234def5678abc1234def5678abc1234def567",
    "path": "/nix/store/abc123-source",
    "locked": {
        "rev": "abc1234def5678abc1234def5678abc1234def567",
        "ref": "main",
        "lastModified": 1700000000,
    },
}


def _mock_args(**kwargs: object) -> MagicMock:
    args = MagicMock()
    args.secrets = Path("/tmp/secrets.json")  # noqa: S108
    args.debug = True
    args.rev = None
    args.branch = None
    args.repo = None
    args.path = Path()
    for k, v in kwargs.items():
        setattr(args, k, v)
    return args


class TestRunCommandFlakeRef:
    def test_flake_ref_resolves_and_splits_effect(self) -> None:
        args = _mock_args(effect="github:my-org/my-repo/main#deploy")

        with (
            patch(
                "buildbot_effects.cli.resolve_flake",
                return_value=FAKE_METADATA,
            ) as mock_resolve,
            patch(
                "buildbot_effects.cli.instantiate_effects", return_value=""
            ) as mock_inst,
        ):
            run_command(args)

        mock_resolve.assert_called_once_with("github:my-org/my-repo/main", debug=True)
        assert mock_inst.call_args[0][0] == "deploy"

    def test_plain_effect_skips_resolution(self) -> None:
        args = _mock_args(effect="deploy")

        with (
            patch("buildbot_effects.cli.resolve_flake") as mock_resolve,
            patch(
                "buildbot_effects.cli.instantiate_effects", return_value=""
            ) as mock_inst,
        ):
            run_command(args)

        mock_resolve.assert_not_called()
        assert mock_inst.call_args[0][0] == "deploy"

    def test_flake_ref_without_fragment_errors(self) -> None:
        args = _mock_args(effect="git+file:///some/repo")

        with pytest.raises(SystemExit, match="1"):
            run_command(args)
