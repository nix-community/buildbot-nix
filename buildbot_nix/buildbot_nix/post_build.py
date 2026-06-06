"""Post-build steps: generic commands run after each successful
attribute build (cachix/niks3 uploads and operator-defined steps).

Ported from PostBuildStep.to_buildstep: instead of buildbot's
Interpolate, the engine substitutes the placeholder forms actually used
by the NixOS modules — `%(prop:NAME)s` (see `build_props` for the
available properties) and `%(secret:NAME)s` (files under
$CREDENTIALS_DIRECTORY). `warn_only` steps log failures without
failing the attribute.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .config import Interpolate, PostBuildStep
    from .events import ChangeEvent
    from .models import NixEvalJobSuccess

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"%\((prop|secret):([^)]+)\)s")


class InterpolationError(Exception):
    pass


def build_props(event: ChangeEvent, job: NixEvalJobSuccess) -> dict[str, str]:
    """Properties available to %(prop:...)s in post-build steps."""
    return {
        "attr": job.attr,
        "out_path": job.outputs.get("out") or "",
        "drv_path": job.drvPath,
        "system": job.system,
        "project": event.project.name,
        "branch": event.branch,
        "revision": event.commit_sha,
        "pr_number": str(event.pr_number or ""),
        "default_branch": event.project.default_branch,
    }


def read_secret(name: str) -> str:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory is None:
        msg = f"secret {name!r} requested but $CREDENTIALS_DIRECTORY is not set"
        raise InterpolationError(msg)
    path = Path(directory) / name
    try:
        return path.read_text().rstrip()
    except OSError as e:
        msg = f"failed to read secret {name!r}: {e}"
        raise InterpolationError(msg) from e


def interpolate(value: str | Interpolate, props: Mapping[str, str]) -> str:
    """Substitute %(prop:...)s and %(secret:...)s placeholders."""
    if isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        kind, name = match.group(1), match.group(2)
        if kind == "prop":
            if name not in props:
                msg = f"unknown build property {name!r} in post-build step"
                raise InterpolationError(msg)
            return props[name]
        return read_secret(name)

    return _PLACEHOLDER_RE.sub(replace, value.value)


@dataclass
class PostBuildStepResult:
    name: str
    success: bool
    warn_only: bool
    output: str

    @property
    def failed(self) -> bool:
        """Failure that should fail the attribute (warn-only excluded)."""
        return not self.success and not self.warn_only


async def run_post_build_step(
    step: PostBuildStep,
    props: Mapping[str, str],
    cwd: Path,
) -> PostBuildStepResult:
    try:
        command = [interpolate(arg, props) for arg in step.command]
        # Inherit the service environment (HOME, XDG_*, ...): tools like
        # cachix and ssh-based uploads need it, and the NixOS module
        # deliberately sets HOME for the service.
        env = {
            **os.environ,
            **{
                key: interpolate(value, props)
                for key, value in step.environment.items()
            },
        }
    except InterpolationError as e:
        return PostBuildStepResult(
            name=step.name, success=False, warn_only=step.warn_only, output=str(e)
        )

    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode(errors="replace")
    success = proc.returncode == 0
    if not success:
        logger.log(
            logging.WARNING if step.warn_only else logging.ERROR,
            "post-build step failed",
            extra={"step": step.name, "returncode": proc.returncode},
        )
    return PostBuildStepResult(
        name=step.name, success=success, warn_only=step.warn_only, output=output
    )


async def run_post_build_steps(
    steps: list[PostBuildStep],
    props: Mapping[str, str],
    cwd: Path,
) -> list[PostBuildStepResult]:
    """Run all steps in order. A hard (non-warn-only) failure fails the
    attribute but does not stop the sequence, matching buildbot's
    flunkOnFailure (not haltOnFailure) behavior."""
    results = []
    for step in steps:
        result = await run_post_build_step(step, props, cwd)
        results.append(result)
    return results
