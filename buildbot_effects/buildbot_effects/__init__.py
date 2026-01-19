from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from contextlib import contextmanager
from tempfile import NamedTemporaryFile
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from .options import EffectsOptions


class BuildbotEffectsError(Exception):
    pass


def run(
    cmd: list[str],
    stdin: int | IO[str] | None = None,
    stdout: int | IO[str] | None = None,
    stderr: int | IO[str] | None = None,
    *,
    debug: bool = True,
) -> subprocess.CompletedProcess[str]:
    if debug:
        print("$", shlex.join(cmd), file=sys.stderr)
    return subprocess.run(
        cmd,
        check=True,
        text=True,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
    )


def git_command(args: list[str], path: Path, *, debug: bool = False) -> str:
    cmd = ["git", "-C", str(path), *args]
    proc = run(cmd, stdout=subprocess.PIPE, debug=debug)
    return proc.stdout.strip()


def get_git_rev(path: Path) -> str:
    return git_command(["rev-parse", "--verify", "HEAD"], path)


def get_git_branch(path: Path) -> str:
    return git_command(["rev-parse", "--abbrev-ref", "HEAD"], path)


def get_git_remote_url(path: Path) -> str | None:
    try:
        return git_command(["remote", "get-url", "origin"], path)
    except subprocess.CalledProcessError:
        return None


def git_get_tag(path: Path, rev: str) -> str | None:
    tags = git_command(["tag", "--points-at", rev], path)
    if tags:
        return tags.splitlines()[1]
    return None


def effects_args(opts: EffectsOptions) -> dict[str, Any]:
    rev = opts.rev or get_git_rev(opts.path)
    short_rev = rev[:7]
    branch = opts.branch or get_git_branch(opts.path)
    repo = opts.repo or opts.path.name
    tag = opts.tag or git_get_tag(opts.path, rev)
    url = opts.url or get_git_remote_url(opts.path)
    primary_repo = {
        "name": repo,
        "branch": branch,
        # TODO: support ref
        "ref": None,
        "tag": tag,
        "rev": rev,
        "shortRev": short_rev,
        "remoteHttpUrl": url,
    }
    return {
        "primaryRepo": primary_repo,
        **primary_repo,
    }


def nix_command(*args: str) -> list[str]:
    return ["nix", "--extra-experimental-features", "nix-command flakes", *args]


def effect_function(opts: EffectsOptions) -> str:
    args = effects_args(opts)
    rev = args["rev"]
    escaped_args = json.dumps(json.dumps(args))
    url = json.dumps(f"git+file://{opts.path}?rev={rev}#")
    return f"""
      let
        flake = builtins.getFlake {url};
        outputName = if flake.outputs ? herculesCI then
          "herculesCI"
        else if flake.outputs ? effects then
          "effects"
        else
          null;
      in
        if outputName == null then
          {{}}
        else
          (flake.outputs.${{outputName}}
            (builtins.fromJSON {escaped_args})).onPush.default.outputs.effects or {{}}
    """


def list_effects(opts: EffectsOptions) -> list[str]:
    cmd = nix_command(
        "eval",
        "--json",
        "--expr",
        f"builtins.attrNames ({effect_function(opts)})",
    )
    proc = run(cmd, stdout=subprocess.PIPE, debug=opts.debug)
    return json.loads(proc.stdout)


def instantiate_effects(effect: str, opts: EffectsOptions) -> str:
    cmd = [
        "nix-instantiate",
        "--expr",
        f"({effect_function(opts)}).{effect}",
    ]
    proc = run(cmd, stdout=subprocess.PIPE, debug=opts.debug)
    return proc.stdout.rstrip()


def parse_derivation(path: str, *, debug: bool = False) -> dict[str, Any]:
    cmd = [
        "nix",
        "--extra-experimental-features",
        "nix-command flakes",
        "derivation",
        "show",
        f"{path}^*",
    ]
    proc = run(cmd, stdout=subprocess.PIPE, debug=debug)
    return json.loads(proc.stdout)


def env_args(env: dict[str, str], clear_env: set[str]) -> list[str]:
    result = []
    for k, v in env.items():
        result.append("--setenv")
        result.append(f"{k}")
        result.append(f"{v}")
    for k in clear_env:
        result.append("--unsetenv")
        result.append(f"{k}")
    return result


@contextmanager
def pipe() -> Iterator[tuple[IO[str], IO[str]]]:
    r, w = os.pipe()
    r_file = os.fdopen(r, "r")
    w_file = os.fdopen(w, "w")
    try:
        yield r_file, w_file
    finally:
        r_file.close()
        w_file.close()


def run_effects(
    drv_path: str,
    drv: dict[str, Any],
    secrets: dict[str, Any] | None = None,
    *,
    debug: bool = False,
) -> None:
    if secrets is None:
        secrets = {}
    builder = drv["builder"]
    args = drv["args"]
    sandboxed_cmd = [
        builder,
        *args,
    ]
    env = {}
    env["IN_HERCULES_CI_EFFECT"] = "true"
    env["HERCULES_CI_SECRETS_JSON"] = "/run/secrets.json"
    env["NIX_BUILD_TOP"] = "/build"
    env["TMPDIR"] = "/tmp"  # noqa: S108
    env["NIX_REMOTE"] = "daemon"
    clear_env = set()
    clear_env.add("TMP")
    clear_env.add("TEMP")
    clear_env.add("TEMPDIR")
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        msg = "bwrap' executable not found"
        raise BuildbotEffectsError(msg)

    # Mirrors hercules-ci implementation: https://github.com/hercules-ci/hercules-ci-agent/blob/57c564298bafde509bd23f4d5862574c94be01ba/hercules-ci-agent/src/Hercules/Effect.hs#L285
    bubblewrap_cmd = [
        "nix",
        "develop",
        "-i",
        f"{drv_path}^*",
        "-c",
        bwrap,
        "--unshare-all",
        "--share-net",
        "--new-session",
        "--die-with-parent",
        "--dir",
        "/build",
        "--chdir",
        "/build",
        "--tmpfs",
        "/tmp",  # noqa: S108
        "--tmpfs",
        "/build",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--uid",
        "0",
        "--gid",
        "0",
        "--ro-bind",
        "/etc/resolv.conf",
        "/etc/resolv.conf",
        "--ro-bind",
        "/nix/store",
        "/nix/store",
        "--hostname",
        "hercules-ci",
        "--bind",
        "/nix/var/nix/daemon-socket/socket",
        "/nix/var/nix/daemon-socket/socket",
    ]

    with NamedTemporaryFile() as tmp:
        secrets = secrets.copy()
        secrets["hercules-ci"] = {"data": {"token": "dummy"}}
        tmp.write(json.dumps(secrets).encode())
        tmp.flush()
        bubblewrap_cmd.extend(
            [
                "--ro-bind",
                tmp.name,
                "/run/secrets.json",
            ],
        )
        bubblewrap_cmd.extend(env_args(env, clear_env))
        bubblewrap_cmd.append("--")
        bubblewrap_cmd.extend(sandboxed_cmd)
        with pipe() as (_r_file, _w_file):
            if debug:
                print("$", shlex.join(bubblewrap_cmd), file=sys.stderr)
            proc = subprocess.run(
                bubblewrap_cmd,
                check=False,
                stdin=subprocess.DEVNULL,
            )
            if proc.returncode != 0:
                msg = f"command failed with exit code {proc.returncode}"
                raise BuildbotEffectsError(msg)
