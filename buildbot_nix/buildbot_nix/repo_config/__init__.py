import tomllib
from tomllib import TOMLDecodeError
from typing import TYPE_CHECKING, Self

from buildbot.process.buildstep import BuildStep, ShellMixin
from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from buildbot.process.log import StreamLog


class BuildStepShellMixin(BuildStep, ShellMixin):
    pass


class RepoConfig(BaseModel):
    branches: list[str]


class BranchConfig(BaseModel):
    lock_file: str = "flake.lock"
    attribute: str = "checks"

    @classmethod
    async def extract_during_step(cls, buildstep: BuildStepShellMixin) -> Self:
        stdio: StreamLog = await buildstep.addLog("stdio")
        cmd = await buildstep.makeRemoteShellCommand(
            collectStdout=True,
            collectStderr=True,
            stdioLogName=None,
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
            stdio.addStderr(
                f"Failed to read repository local configuration, {cmd.stderr}.\n"
            )
            return cls()
        try:
            return cls.model_validate(tomllib.loads(cmd.stdout))
        except ValidationError as e:
            stdio.addStderr(f"Failed to read repository local configuration, {e}.\n")
            return cls()
        except TOMLDecodeError as e:
            stdio.addStderr(f"Failed to read repository local configuration, {e}.\n")
            return cls()
