"""Tests for CLI flake ref support."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from buildbot_effects.cli import run_command
from buildbot_effects.options import EffectsOptions

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


def _base_options() -> EffectsOptions:
    return EffectsOptions(
        secrets=Path("/tmp/secrets.json"),  # noqa: S108
        debug=True,
    )


class TestRunCommandFlakeRef:
    def test_flake_ref_resolves_and_splits_effect(self) -> None:
        args = MagicMock()
        args.effect = "github:my-org/my-repo/main#deploy"

        with (
            patch(
                "buildbot_effects.cli.resolve_flake",
                return_value=FAKE_METADATA,
            ) as mock_resolve,
            patch(
                "buildbot_effects.cli.instantiate_effects", return_value=""
            ) as mock_inst,
        ):
            run_command(args, _base_options())

        mock_resolve.assert_called_once_with("github:my-org/my-repo/main", debug=True)
        assert mock_inst.call_args[0][0] == "deploy"

    def test_plain_effect_skips_resolution(self) -> None:
        args = MagicMock()
        args.effect = "deploy"

        with (
            patch("buildbot_effects.cli.resolve_flake") as mock_resolve,
            patch(
                "buildbot_effects.cli.instantiate_effects", return_value=""
            ) as mock_inst,
        ):
            run_command(args, _base_options())

        mock_resolve.assert_not_called()
        assert mock_inst.call_args[0][0] == "deploy"
