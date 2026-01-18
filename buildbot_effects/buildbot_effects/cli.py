from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import (
    instantiate_effects,
    instantiate_scheduled_effect,
    list_effects,
    list_scheduled_effects,
    parse_derivation,
    run_effects,
)
from .options import EffectsOptions


def list_command(_args: argparse.Namespace, options: EffectsOptions) -> None:
    json.dump(list_effects(options), fp=sys.stdout, indent=2)


def run_command(args: argparse.Namespace, options: EffectsOptions) -> None:
    effect = args.effect
    drv_path = instantiate_effects(effect, options)
    if drv_path == "":
        print(f"Effect {effect} not found or not runnable for {options}")
        return
    drvs = parse_derivation(drv_path)
    if "derivations" in drvs:
        drvs = drvs["derivations"]
    drv = next(iter(drvs.values()))

    secrets = json.loads(options.secrets.read_text()) if options.secrets else {}
    run_effects(drv_path, drv, secrets=secrets)


def run_all_command(_args: argparse.Namespace, _options: EffectsOptions) -> None:
    print("TODO")


def list_schedules_command(_args: argparse.Namespace, options: EffectsOptions) -> None:
    """List all scheduled effects defined in the flake."""
    json.dump(list_scheduled_effects(options), fp=sys.stdout, indent=2)


def run_scheduled_command(args: argparse.Namespace, options: EffectsOptions) -> None:
    """Run a specific effect from a schedule."""
    schedule_name = args.schedule_name
    effect = args.effect
    drv_path = instantiate_scheduled_effect(schedule_name, effect, options)
    if drv_path == "":
        print(
            f"Scheduled effect {schedule_name}/{effect} not found or not runnable for {options}"
        )
        return
    drvs = parse_derivation(drv_path)
    if "derivations" in drvs:
        drvs = drvs["derivations"]
    drv = next(iter(drvs.values()))

    secrets = json.loads(options.secrets.read_text()) if options.secrets else {}
    run_effects(drv_path, drv, secrets=secrets)


def parse_args() -> tuple[argparse.Namespace, EffectsOptions]:
    parser = argparse.ArgumentParser(description="Run effects from a hercules-ci flake")
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
        help="List available effects",
    )
    list_parser.set_defaults(command=list_command)
    run_parser = subparser.add_parser(
        "run",
        help="Run an effect",
    )
    run_parser.set_defaults(command=run_command)
    run_parser.add_argument(
        "effect",
        help="Effect to run",
    )
    run_all_parser = subparser.add_parser(
        "run-all",
        help="Run all effects",
    )
    run_all_parser.set_defaults(command=run_all_command)

    list_schedules_parser = subparser.add_parser(
        "list-schedules",
        help="List all scheduled effects defined in the flake",
    )
    list_schedules_parser.set_defaults(command=list_schedules_command)

    run_scheduled_parser = subparser.add_parser(
        "run-scheduled",
        help="Run a specific effect from a schedule",
    )
    run_scheduled_parser.set_defaults(command=run_scheduled_command)
    run_scheduled_parser.add_argument(
        "schedule_name",
        help="Name of the schedule (from onSchedule.<name>)",
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
