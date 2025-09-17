from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import instantiate_effects, list_effects, parse_derivation, run_effects
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
    drv = next(iter(drvs.values()))

    secrets = json.loads(options.secrets.read_text()) if options.secrets else {}
    run_effects(drv_path, drv, secrets=secrets)


def run_all_command(_args: argparse.Namespace, _options: EffectsOptions) -> None:
    print("TODO")


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
