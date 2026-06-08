from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import (
    instantiate_effects,
    instantiate_scheduled_effect,
    list_effects,
    list_scheduled_effects,
    nix_command,
    parse_derivation,
    run_effects,
    select_mounts,
    select_secrets,
)
from .options import EffectsOptions


def resolve_flake(flake_ref: str, *, debug: bool = False) -> dict[str, Any]:
    """Run `nix flake metadata --json` and return the parsed JSON."""
    cmd = nix_command("flake", "metadata", "--json", flake_ref)
    if debug:
        print("$", shlex.join(cmd), file=sys.stderr)
    try:
        proc = subprocess.run(cmd, check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        msg = f"Failed to resolve flake reference '{flake_ref}'\n"
        msg += f"Command: {shlex.join(cmd)}\n"
        msg += f"Exit code: {e.returncode}\n"
        msg += f"Output: {e.stderr}\n"
        raise RuntimeError(msg) from e
    return json.loads(proc.stdout)


def options_from_flake_ref(flake_ref: str, base: EffectsOptions) -> EffectsOptions:
    """Resolve a flake reference to EffectsOptions via nix flake metadata."""
    meta = resolve_flake(flake_ref, debug=base.debug)
    locked = meta.get("locked", {})
    # lockedUrl can be null in JSON (None in Python), fall back to url
    locked_url = meta.get("lockedUrl") or meta.get("url", "")
    rev = locked.get("rev") or locked.get("dirtyRev")
    return EffectsOptions(
        secrets=base.secrets,
        path=Path(meta.get("path", "")),
        repo=base.repo,
        rev=rev,
        branch=locked.get("ref") or base.branch,
        tag=base.tag,
        url=meta.get("resolvedUrl", meta.get("url", "")),
        locked_url=locked_url,
        default_branch=base.default_branch,
        git_token_file=base.git_token_file,
        mountables_file=base.mountables_file,
        api_base_url=base.api_base_url,
        task_token_file=base.task_token_file,
        project_id=base.project_id,
        project_path=base.project_path,
        extra_nix_options=base.extra_nix_options,
        debug=base.debug,
        extra_sandbox_paths=base.extra_sandbox_paths,
    )


def _options_from_args(args: argparse.Namespace) -> EffectsOptions:
    return EffectsOptions(
        secrets=args.secrets,
        branch=args.branch,
        rev=args.rev,
        repo=args.repo or "",
        tag=args.tag,
        path=args.path.resolve(),
        default_branch=args.default_branch,
        git_token_file=args.git_token_file,
        mountables_file=args.mountables_file,
        api_base_url=args.api_base_url,
        task_token_file=args.task_token_file,
        project_id=args.project_id,
        project_path=args.project_path,
        extra_nix_options=args.extra_nix_option,
        debug=args.debug,
        extra_sandbox_paths=args.extra_sandbox_path,
    )


def list_command(args: argparse.Namespace) -> None:
    options = _options_from_args(args)
    if args.flake_ref:
        options = options_from_flake_ref(args.flake_ref, options)
    json.dump(list_effects(options), fp=sys.stdout, indent=2)


def _run_effect_derivation(drv_path: str, options: EffectsOptions) -> None:
    drvs = parse_derivation(drv_path, debug=options.debug)
    if "derivations" in drvs:
        drvs = drvs["derivations"]
    drv = next(iter(drvs.values()))

    secrets = json.loads(options.secrets.read_text()) if options.secrets else {}
    run_effects(
        drv_path,
        drv,
        secrets=select_secrets(drv, secrets, options),
        bind_mounts=select_mounts(drv, options),
        opts=options,
        debug=options.debug,
        extra_sandbox_paths=options.extra_sandbox_paths,
    )


def _split_flake_ref(
    name: str, options: EffectsOptions, usage: str
) -> tuple[str, EffectsOptions]:
    """Split flakeref#name syntax (e.g. github:org/repo/branch#my-effect);
    reject a bare flake ref, which would otherwise be treated as a name."""
    if "#" in name:
        flake_ref, _, name = name.partition("#")
        return name, options_from_flake_ref(flake_ref, options)
    if ":" in name or name.startswith("/"):
        print(
            f"error: '{name}' looks like a flake reference but is missing the"
            f" '#' fragment\n  usage: {usage}",
            file=sys.stderr,
        )
        sys.exit(1)
    return name, options


def run_command(args: argparse.Namespace) -> None:
    options = _options_from_args(args)
    effect, options = _split_flake_ref(
        args.effect, options, f"buildbot-effects run {args.effect}#<effect-name>"
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        gcroot = Path(tmpdir) / "result"
        drv_path = instantiate_effects(effect, options, gcroot)
        if drv_path == "":
            print(
                f"Effect {effect} not found or not runnable for {options}",
                file=sys.stderr,
            )
            sys.exit(1)
        _run_effect_derivation(drv_path, options)


def list_schedules_command(args: argparse.Namespace) -> None:
    """List all scheduled effects defined in the flake."""
    options = _options_from_args(args)
    if args.flake_ref:
        options = options_from_flake_ref(args.flake_ref, options)
    json.dump(list_scheduled_effects(options), fp=sys.stdout, indent=2)


def run_scheduled_command(args: argparse.Namespace) -> None:
    """Run a specific effect from a schedule."""
    options = _options_from_args(args)
    effect = args.effect
    schedule_name, options = _split_flake_ref(
        args.schedule_name,
        options,
        f"buildbot-effects run-scheduled {args.schedule_name}#<schedule-name> <effect>",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        gcroot = Path(tmpdir) / "result"
        drv_path = instantiate_scheduled_effect(schedule_name, effect, options, gcroot)
        if drv_path == "":
            print(
                f"Scheduled effect {schedule_name}/{effect} not found or not runnable"
                f" for {options}",
                file=sys.stderr,
            )
            sys.exit(1)
        _run_effect_derivation(drv_path, options)


def _key_value(option: str) -> tuple[str, str]:
    key, sep, value = option.partition("=")
    if not sep or not key:
        msg = f"expected KEY=VALUE, got {option!r}"
        raise argparse.ArgumentTypeError(msg)
    return (key, value)


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    """Add flags shared by all subcommands."""
    parser.add_argument(
        "--rev",
        type=str,
        help="Git revision to use",
    )
    parser.add_argument(
        "--branch",
        type=str,
        help="Git branch to use",
    )
    parser.add_argument(
        "--repo",
        type=str,
        help="Git repo name",
    )
    parser.add_argument(
        "--tag",
        type=str,
        help="Git tag of the revision (for isTag secret conditions)",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=Path(),
        help="Path to the repository",
    )
    parser.add_argument(
        "--debug",
        default=False,
        action="store_true",
        help="Enable debug mode (may leak secrets such as GITHUB_TOKEN)",
    )
    parser.add_argument(
        "--default-branch",
        type=str,
        help="Default branch of the repository (for secret conditions)",
    )
    parser.add_argument(
        "--git-token-file",
        type=Path,
        help="File with a forge token for GitToken secret references",
    )
    parser.add_argument(
        "--mountables-file",
        type=Path,
        help="JSON file with mountables effects may request via __hci_effect_mounts",
    )
    parser.add_argument(
        "--api-base-url",
        type=str,
        help="Hercules state API base URL (HERCULES_CI_API_BASE_URL)",
    )
    parser.add_argument(
        "--task-token-file",
        type=Path,
        help="File with the bearer token for the state API",
    )
    parser.add_argument(
        "--project-id",
        type=str,
        help="Value for HERCULES_CI_PROJECT_ID",
    )
    parser.add_argument(
        "--project-path",
        type=str,
        help="Value for HERCULES_CI_PROJECT_PATH",
    )
    parser.add_argument(
        "--extra-nix-option",
        action="append",
        default=[],
        type=_key_value,
        metavar="KEY=VALUE",
        help="nix option for the effect's private daemon",
    )
    parser.add_argument(
        "--extra-sandbox-path",
        type=Path,
        action="append",
        default=[],
        help="Path that should be included in the sandbox from the host.",
    )
    parser.add_argument(
        "--secrets",
        type=Path,
        help="Path to a json file with secrets",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run effects from a hercules-ci flake",
        epilog=(
            "Flake reference syntax:\n"
            "  Commands accept flake references to operate on remote repositories\n"
            "  without requiring a local checkout:\n\n"
            "  buildbot-effects run github:org/repo/branch#my-effect\n"
            "  buildbot-effects list github:org/repo/branch\n"
            "  buildbot-effects run-scheduled github:org/repo#schedule effect\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparser = parser.add_subparsers(
        dest="command",
        required=True,
        help="Command to run",
    )

    list_parser = subparser.add_parser(
        "list",
        help="List available effects (optionally from a flake reference)",
    )
    _add_common_flags(list_parser)
    list_parser.set_defaults(func=list_command)
    list_parser.add_argument(
        "flake_ref",
        nargs="?",
        help="Flake reference (e.g. github:org/repo/branch)",
    )

    run_parser = subparser.add_parser(
        "run",
        help="Run an effect (supports flakeref#effect syntax)",
    )
    _add_common_flags(run_parser)
    run_parser.set_defaults(func=run_command)
    run_parser.add_argument(
        "effect",
        help="Effect to run, or flakeref#effect (e.g. github:org/repo/branch#deploy)",
    )

    list_schedules_parser = subparser.add_parser(
        "list-schedules",
        help="List all scheduled effects (optionally from a flake reference)",
    )
    _add_common_flags(list_schedules_parser)
    list_schedules_parser.set_defaults(func=list_schedules_command)
    list_schedules_parser.add_argument(
        "flake_ref",
        nargs="?",
        help="Flake reference (e.g. github:org/repo/branch)",
    )

    run_scheduled_parser = subparser.add_parser(
        "run-scheduled",
        help="Run a specific effect from a schedule",
    )
    _add_common_flags(run_scheduled_parser)
    run_scheduled_parser.set_defaults(func=run_scheduled_command)
    run_scheduled_parser.add_argument(
        "schedule_name",
        help="Schedule name, or flakeref#schedule (e.g. github:org/repo#my-schedule)",
    )
    run_scheduled_parser.add_argument(
        "effect",
        help="Effect to run within the schedule",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.func(args)
