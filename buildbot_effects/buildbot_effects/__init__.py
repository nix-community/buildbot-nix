from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import tempfile
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .options import EffectsOptions


from .daemon_proxy import nix_daemon_proxy
from .secrets import (
    SecretContext,
    SecretsError,
    check_mounts,
    gather_secrets,
    parse_secrets_map,
)


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
        return tags.splitlines()[0]
    return None


def get_git_branch_rev(path: Path, branch: str) -> str:
    """Resolve the tip commit of a given branch."""
    return git_command(["rev-parse", "--verify", branch], path)


def _is_git_repo(path: Path) -> bool:
    """Check if path is inside a git repository."""
    try:
        cmd = ["git", "-C", str(path), "rev-parse", "--git-dir"]
        subprocess.run(
            cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        return False
    return True


def effects_args(opts: EffectsOptions) -> dict[str, Any]:
    has_git = _is_git_repo(opts.path)
    if opts.rev:
        rev = opts.rev
    elif opts.branch and has_git:
        rev = get_git_branch_rev(opts.path, opts.branch)
    elif has_git:
        rev = get_git_rev(opts.path)
    else:
        msg = "No --rev specified and path is not a git repository"
        raise BuildbotEffectsError(msg)
    short_rev = rev[:7]
    branch = opts.branch or (get_git_branch(opts.path) if has_git else None)
    repo = opts.repo or opts.path.name
    tag = opts.tag or (git_get_tag(opts.path, rev) if has_git else None)
    url = opts.url or (get_git_remote_url(opts.path) if has_git else None)
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


def _flake_url(opts: EffectsOptions, rev: str) -> str:
    """Return the Nix flake URL to use with builtins.getFlake.

    When a locked_url is available (from a resolved remote flake ref),
    use it directly. Otherwise fall back to constructing a git+file:// URL
    from the local path.
    """
    if opts.locked_url:
        return opts.locked_url
    return f"git+file://{opts.path}?rev={rev}#"


def effect_function(opts: EffectsOptions) -> str:
    args = effects_args(opts)
    rev = args["rev"]
    escaped_args = json.dumps(json.dumps(args))
    url = json.dumps(_flake_url(opts, rev))
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


def scheduled_effect_function(opts: EffectsOptions) -> str:
    """Generate Nix expression to evaluate onSchedule attributes."""
    args = effects_args(opts)
    rev = args["rev"]
    escaped_args = json.dumps(json.dumps(args))
    url = json.dumps(_flake_url(opts, rev))
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
            (builtins.fromJSON {escaped_args})).onSchedule or {{}}
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


def list_scheduled_effects(opts: EffectsOptions) -> dict[str, Any]:
    """List all scheduled effect definitions with their schedules.

    Returns a dict mapping schedule name to {when: {...}, effects: [...]}.
    """
    cmd = nix_command(
        "eval",
        "--json",
        "--expr",
        f"""
        let
          schedules = {scheduled_effect_function(opts)};
        in
          builtins.mapAttrs (name: schedule: {{
            when = schedule.when or {{}};
            effects = builtins.attrNames (schedule.outputs.effects or {{}});
          }}) schedules
        """,
    )
    proc = run(cmd, stdout=subprocess.PIPE, debug=opts.debug)
    return json.loads(proc.stdout)


def _instantiate(expr: str, opts: EffectsOptions, gcroot: Path) -> str:
    cmd = [
        "nix-instantiate",
        "--add-root",
        str(gcroot),
        "--expr",
        expr,
    ]
    proc = run(cmd, stdout=subprocess.PIPE, debug=opts.debug)
    # --add-root prints the symlink path; resolve to the actual store path
    return str(Path(proc.stdout.rstrip()).resolve())


def instantiate_effects(effect: str, opts: EffectsOptions, gcroot: Path) -> str:
    return _instantiate(
        f"let e = ({effect_function(opts)}).{effect}; in if e ? run then e.run else e",
        opts,
        gcroot,
    )


def instantiate_scheduled_effect(
    schedule_name: str, effect: str, opts: EffectsOptions, gcroot: Path
) -> str:
    """Instantiate a specific effect from a schedule."""
    return _instantiate(
        f"({scheduled_effect_function(opts)}).{schedule_name}.outputs.effects.{effect}",
        opts,
        gcroot,
    )


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


def secret_context(opts: EffectsOptions) -> SecretContext:
    branch = opts.branch or ""
    return SecretContext(
        owner_name=(opts.repo or "").split("/")[0],
        repo_name=(opts.repo or "").rsplit("/", 1)[-1],
        is_default_branch=opts.default_branch is not None
        and branch == opts.default_branch,
        ref=f"refs/tags/{opts.tag}" if opts.tag else f"refs/heads/{branch}",
    )


def select_mounts(
    drv: dict[str, Any], opts: EffectsOptions
) -> list[tuple[str, str, bool]]:
    """Bind mounts requested via __hci_effect_mounts, validated
    against the configured mountables."""
    raw = drv.get("env", {}).get("__hci_effect_mounts")
    if not raw:
        return []
    try:
        mounts = json.loads(raw)
    except json.JSONDecodeError as e:
        msg = f"could not parse __hci_effect_mounts in the derivation: {e}"
        raise SecretsError(msg) from e
    if not isinstance(mounts, dict) or not all(
        isinstance(name, str) for name in mounts.values()
    ):
        msg = (
            "__hci_effect_mounts in the derivation must be a JSON object "
            "mapping mount paths to mountable names"
        )
        raise SecretsError(msg)
    mountables: dict[str, Any] = {}
    if opts.mountables_file is not None:
        mountables = json.loads(opts.mountables_file.read_text())
    return check_mounts(mountables, secret_context(opts), mounts)


def select_secrets(
    drv: dict[str, Any],
    secrets: dict[str, Any],
    opts: EffectsOptions,
) -> dict[str, Any]:
    """Hercules semantics when the effect declares secretsMap;
    whole-file passthrough otherwise (legacy buildbot-nix behavior)."""
    secrets_map = parse_secrets_map(drv.get("env", {}))
    if not secrets_map:
        return secrets
    ctx = secret_context(opts)
    git_token = None
    if opts.git_token_file is not None:
        git_token = opts.git_token_file.read_text().strip()
    return gather_secrets(secrets_map, secrets, ctx, git_token)


def sandbox_env(drv_env: dict[str, str]) -> dict[str, str]:
    """Environment matching hercules-ci-agent: every temp variable
    points at the disk-backed /build, plus its fixed env. HOME is
    overridable by the derivation but never inherited from the host
    (nix develop would leak the service user's)."""
    return {
        "HOME": drv_env.get("HOME", "/homeless-shelter"),
        "IN_HERCULES_CI_EFFECT": "true",
        "HERCULES_CI_SECRETS_JSON": "/run/secrets.json",
        "NIX_BUILD_TOP": "/build",
        "TMPDIR": "/build",
        "TMP": "/build",
        "TEMP": "/build",
        "TEMPDIR": "/build",
        "NIX_REMOTE": "daemon",
        "NIX_LOG_FD": "2",
        "TERM": "xterm-256color",
    }


def pass_as_file_env(
    drv_env: dict[str, str], build_dir: Path
) -> tuple[dict[str, str], set[str]]:
    """Nix's passAsFile: write each listed variable into the build
    directory and point <name>Path at it. The agent never implemented
    this, but plain runCommand effects need it for buildCommand."""
    env: dict[str, str] = {}
    clear: set[str] = set()
    for name in drv_env.get("passAsFile", "").split():
        (build_dir / f".attr-{name}").write_text(drv_env.get(name, ""))
        env[f"{name}Path"] = f"/build/.attr-{name}"
        clear.add(name)
    return env, clear


def virtual_ids(drv_env: dict[str, str]) -> tuple[int, int]:
    """`__hci_effect_virtual_uid`/`gid` from the derivation; the
    agent's defaults are 0/uid."""

    def parse(name: str, default: int) -> int:
        value = drv_env.get(name)
        if not value:
            return default
        try:
            return int(value)
        except ValueError as e:
            msg = f"invalid {name} in the derivation: {value!r} is not an integer"
            raise BuildbotEffectsError(msg) from e

    uid = parse("__hci_effect_virtual_uid", 0)
    gid = parse("__hci_effect_virtual_gid", uid)
    return uid, gid


def _task_env(
    drv_env: dict[str, str], opts: EffectsOptions | None
) -> tuple[dict[str, str], str]:
    """Sandbox env plus the state API token ("dummy" without one,
    like the agent's local mode)."""
    env = sandbox_env(drv_env)
    token = "dummy"  # noqa: S105 (placeholder, not a credential)
    if opts is not None:
        if opts.api_base_url:
            env["HERCULES_CI_API_BASE_URL"] = opts.api_base_url
        if opts.project_id:
            env["HERCULES_CI_PROJECT_ID"] = opts.project_id
        if opts.project_path:
            env["HERCULES_CI_PROJECT_PATH"] = opts.project_path
        if opts.task_token_file is not None:
            token = opts.task_token_file.read_text().strip()
    return env, token


def _work_dirs() -> tuple[Path, Path, Path]:
    work_dir = Path(tempfile.mkdtemp(prefix="effect-"))
    build_dir = work_dir / "build"
    etc_dir = work_dir / "etc"
    build_dir.mkdir()
    etc_dir.mkdir()
    return work_dir, build_dir, etc_dir


def run_effects(  # noqa: PLR0913
    drv_path: str,
    drv: dict[str, Any],
    *,
    secrets: dict[str, Any] | None = None,
    extra_sandbox_paths: list[Path] | None = None,
    bind_mounts: list[tuple[str, str, bool]] | None = None,
    opts: EffectsOptions | None = None,
    debug: bool = False,
) -> None:
    if secrets is None:
        secrets = {}
    if extra_sandbox_paths is None:
        extra_sandbox_paths = []
    if bind_mounts is None:
        bind_mounts = []
    builder = drv["builder"]
    args = drv["args"]
    sandboxed_cmd = [
        builder,
        *args,
    ]
    drv_env = drv.get("env", {})
    env, task_token = _task_env(drv_env, opts)
    uid, gid = virtual_ids(drv_env)
    clear_env: set[str] = set()
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        msg = "bwrap executable not found"
        raise BuildbotEffectsError(msg)
    work_dir, build_dir, etc_dir = _work_dirs()
    pass_env, pass_clear = pass_as_file_env(drv_env, build_dir)
    env.update(pass_env)
    clear_env |= pass_clear
    # Private daemon socket: each connection gets its own untrusted
    # nix-daemon, so effects cannot use trusted-user privileges.
    daemon_socket = work_dir / "daemon-socket"
    proxy: AbstractContextManager[None] = nullcontext()
    if shutil.which("nix-daemon") is not None:
        proxy = nix_daemon_proxy(
            daemon_socket, opts.extra_nix_options if opts is not None else []
        )
    else:
        daemon_socket = Path("/nix/var/nix/daemon-socket/socket")

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
        # Disk-backed like the agent's work dirs: deploys unpack
        # closures that don't fit a tmpfs.
        "--bind",
        str(build_dir),
        "/build",
        "--tmpfs",
        "/tmp",  # noqa: S108
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--uid",
        str(uid),
        "--gid",
        str(gid),
        # Writable /etc like the agent's fresh etc dir; resolv.conf
        # bound inside it from the host.
        "--bind",
        str(etc_dir),
        "/etc",
        "--ro-bind",
        "/etc/resolv.conf",
        "/etc/resolv.conf",
        "--ro-bind",
        "/nix/store",
        "/nix/store",
        *[
            arg
            for path in extra_sandbox_paths
            for arg in ("--ro-bind", str(path), str(path))
        ],
        *[
            arg
            for dest, source, read_only in bind_mounts
            for arg in ("--ro-bind" if read_only else "--bind", source, dest)
        ],
        "--hostname",
        "hercules-ci",
        "--bind",
        str(daemon_socket),
        "/nix/var/nix/daemon-socket/socket",
    ]

    with NamedTemporaryFile() as tmp:
        secrets = secrets.copy()
        secrets["hercules-ci"] = {"data": {"token": task_token}}
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
        if debug:
            print("$", shlex.join(bubblewrap_cmd), file=sys.stderr)
        try:
            with proxy:
                proc = subprocess.run(
                    bubblewrap_cmd,
                    check=False,
                    stdin=subprocess.DEVNULL,
                )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        if proc.returncode != 0:
            msg = f"command failed with exit code {proc.returncode}"
            raise BuildbotEffectsError(msg)
