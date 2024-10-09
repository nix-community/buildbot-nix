import tomllib
from tomllib import TOMLDecodeError
from typing import TYPE_CHECKING, Self

from buildbot.process.buildstep import ShellMixin

if TYPE_CHECKING:
    from buildbot.process.log import Log
from pydantic import BaseModel, ValidationError


class RepoConfig(BaseModel):
    branches: list[str]


class BranchConfig(BaseModel):
    lock_file: str = "flake.lock"
    attribute: str = "checks"

    @classmethod
    async def extract_during_step(cls, buildstep: ShellMixin) -> Self:
        stdio: Log = await buildstep.addLog("stdio")
        cmd = await buildstep.makeRemoteShellCommand(
            collectStdout=True,
            collectStderr=False,
            stdioLogName=None,
            command=["cat", "buildbot-nix.toml"],
        )
        await buildstep.runCommand(cmd)
        if cmd.didFail():
            stdio.addStdout("Failed to read repository local configuration.\n")
            return cls()
        try:
            return cls.model_validate(tomllib.loads(cmd.stdout))
        except ValidationError as e:
            stdio.addStdout(f"Failed to read repository local configuration, {e}.\n")
            return cls()
        except TOMLDecodeError as e:
            stdio.addStdout(f"Failed to read repository local configuration, {e}.\n")
            return cls()
