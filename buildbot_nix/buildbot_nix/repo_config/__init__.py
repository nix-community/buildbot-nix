from __future__ import annotations

import tomllib
from pathlib import Path
from tomllib import TOMLDecodeError
from typing import Self

from buildbot.process.buildstep import BuildStep, ShellMixin
from pydantic import BaseModel, ValidationError

from buildbot_nix.errors import BuildbotNixError


class BuildStepShellMixin(BuildStep, ShellMixin):  # type: ignore[misc]
    pass


def _validate_flake_dir(flake_dir: str) -> None:
    """Validate that flake_dir is a safe relative path within the repo root."""
    resolved = Path(flake_dir).resolve()
    root_dir = Path.cwd().resolve()
    if ":" in flake_dir or not resolved.is_relative_to(root_dir):
        msg = f"Invalid flake_dir {flake_dir}"
        raise BuildbotNixError(msg)


class RepoConfig(BaseModel):
    branches: list[str]


class BranchConfig(BaseModel):
    flake_dir: str = "."
    lock_file: str = "flake.lock"
    attribute: str = "checks"
    effects_on_pull_requests: bool = False
    effects_branches: list[str] = []

    @classmethod
    async def extract_during_step(cls, buildstep: BuildStepShellMixin) -> Self:
        stdio = await buildstep.addLog("stdio")
        cmd = await buildstep.makeRemoteShellCommand(
            collectStdout=True,
            collectStderr=True,
            stdioLogName=None,  # type: ignore[arg-type]
            # TODO: replace this with something like buildbot.steps.transfer.StringUpload
            # in the future... this one doesn't not exist yet.
            command=[
                "sh",
                "-c",
                "if [ -f buildbot-nix.toml ]; then cat buildbot-nix.toml; fi",
            ],
        )
        await buildstep.runCommand(cmd)
        if cmd.didFail():
            stdio.addStderr(  # type: ignore[attr-defined]
                f"Failed to read repository local configuration, {cmd.stderr}.\n"
            )
            return cls()
        try:
            config = cls.model_validate(tomllib.loads(cmd.stdout))
            _validate_flake_dir(config.flake_dir)
        except ValidationError as e:
            stdio.addStderr(  # type: ignore[attr-defined]
                f"Failed to read repository local configuration, {e}.\n"
            )
            return cls()
        except TOMLDecodeError as e:
            stdio.addStderr(  # type: ignore[attr-defined]
                f"Failed to read repository local configuration, {e}.\n"
            )
            return cls()
        return config
