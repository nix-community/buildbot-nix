from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import (
    instantiate_effects,
    instantiate_scheduled_effect,
    list_effects,
    list_scheduled_effects,
    parse_derivation,
    run_effects,
)
from .options import EffectsOptions


def resolve_flake(flake_ref: str, *, debug: bool = False) -> dict[str, Any]:
    """Run `nix flake metadata --json` and return the parsed JSON."""
    cmd = [
        "nix",
        "--extra-experimental-features",
        "nix-command flakes",
        "flake",
        "metadata",
        "--json",
        flake_ref,
    ]
    if debug:
        print("$", shlex.join(cmd), file=sys.stderr)
    proc = subprocess.run(cmd, check=True, text=True, capture_output=True)
    return json.loads(proc.stdout)


def options_from_flake_ref(flake_ref: str, base: EffectsOptions) -> EffectsOptions:
    """Resolve a flake reference to EffectsOptions via nix flake metadata."""
    meta = resolve_flake(flake_ref, debug=base.debug)
    locked = meta.get("locked", {})
    return EffectsOptions(
        secrets=base.secrets,
        path=Path(meta.get("path", "")),
        repo="",
        rev=locked.get("rev"),
        branch=locked.get("ref"),
        url=meta.get("resolvedUrl", meta.get("url", "")),
        locked_url=meta.get("lockedUrl", ""),
        debug=base.debug,
    )


def list_command(_args: argparse.Namespace, options: EffectsOptions) -> None:
    if _args.flake_ref:
        options = options_from_flake_ref(_args.flake_ref, options)
    json.dump(list_effects(options), fp=sys.stdout, indent=2)


def run_command(args: argparse.Namespace, options: EffectsOptions) -> None:
    effect = args.effect

    # Support flakeref#effect syntax: github:org/repo/branch#my-effect
    if "#" in effect:
        flake_ref, _, effect = effect.partition("#")
        options = options_from_flake_ref(flake_ref, options)

    drv_path = instantiate_effects(effect, options)
    if drv_path == "":
        print(f"Effect {effect} not found or not runnable for {options}")
        return
    drvs = parse_derivation(drv_path)
    if "derivations" in drvs:
        drvs = drvs["derivations"]
    drv = next(iter(drvs.values()))

    secrets = json.loads(options.secrets.read_text()) if options.secrets else {}
    run_effects(drv_path, drv, secrets=secrets, debug=options.debug)


def run_all_command(_args: argparse.Namespace, _options: EffectsOptions) -> None:
    print("TODO")


def list_schedules_command(_args: argparse.Namespace, options: EffectsOptions) -> None:
    """List all scheduled effects defined in the flake."""
    if _args.flake_ref:
        options = options_from_flake_ref(_args.flake_ref, options)
    json.dump(list_scheduled_effects(options), fp=sys.stdout, indent=2)


def run_scheduled_command(args: argparse.Namespace, options: EffectsOptions) -> None:
    """Run a specific effect from a schedule."""
    schedule_name = args.schedule_name
    effect = args.effect

    # Support flakeref#schedule syntax: github:org/repo/branch#my-schedule
    if "#" in schedule_name:
        flake_ref, _, schedule_name = schedule_name.partition("#")
        options = options_from_flake_ref(flake_ref, options)

    drv_path = instantiate_scheduled_effect(schedule_name, effect, options)
    if drv_path == "":
        print(
            f"Scheduled effect {schedule_name}/{effect} not found or not runnable"
            f" for {options}"
        )
        return
    drvs = parse_derivation(drv_path)
    if "derivations" in drvs:
        drvs = drvs["derivations"]
    drv = next(iter(drvs.values()))

    secrets = json.loads(options.secrets.read_text()) if options.secrets else {}
    run_effects(drv_path, drv, secrets=secrets, debug=options.debug)


def parse_args() -> tuple[argparse.Namespace, EffectsOptions]:
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
    parser.add_argument(
        "--secrets",
        type=Path,
        help="Path to a json file with secrets",
    )
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
        help="Git repo to prepend to be",
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
    subparser = parser.add_subparsers(
        dest="command",
        required=True,
        help="Command to run",
    )
    list_parser = subparser.add_parser(
        "list",
        help="List available effects (optionally from a flake reference)",
    )
    list_parser.set_defaults(command=list_command)
    list_parser.add_argument(
        "flake_ref",
        nargs="?",
        help="Flake reference (e.g. github:org/repo/branch)",
    )

    run_parser = subparser.add_parser(
        "run",
        help="Run an effect (supports flakeref#effect syntax)",
    )
    run_parser.set_defaults(command=run_command)
    run_parser.add_argument(
        "effect",
        help="Effect to run, or flakeref#effect (e.g. github:org/repo/branch#deploy)",
    )
    run_all_parser = subparser.add_parser(
        "run-all",
        help="Run all effects",
    )
    run_all_parser.set_defaults(command=run_all_command)

    list_schedules_parser = subparser.add_parser(
        "list-schedules",
        help="List all scheduled effects (optionally from a flake reference)",
    )
    list_schedules_parser.set_defaults(command=list_schedules_command)
    list_schedules_parser.add_argument(
        "flake_ref",
        nargs="?",
        help="Flake reference (e.g. github:org/repo/branch)",
    )

    run_scheduled_parser = subparser.add_parser(
        "run-scheduled",
        help="Run a specific effect from a schedule",
    )
    run_scheduled_parser.set_defaults(command=run_scheduled_command)
    run_scheduled_parser.add_argument(
        "schedule_name",
        help="Schedule name, or flakeref#schedule (e.g. github:org/repo#my-schedule)",
    )
    run_scheduled_parser.add_argument(
        "effect",
        help="Effect to run within the schedule",
    )

    args = parser.parse_args()
    opts = EffectsOptions(
        secrets=args.secrets,
        branch=args.branch,
        rev=args.rev,
        repo=args.repo,
        path=args.path.resolve(),
        debug=args.debug,
    )
    return args, opts


def main() -> None:
    args, options = parse_args()
    args.command(args, options)
