import argparse
import json
from collections.abc import Callable
from pathlib import Path

from . import instantiate_effects, list_effects, parse_derivation, run_effects
from .options import EffectsOptions


def list_command(options: EffectsOptions) -> None:
    print(list_effects(options))


def run_command(options: EffectsOptions) -> None:
    drv_path = instantiate_effects(options)
    drvs = parse_derivation(drv_path)
    drv = next(iter(drvs.values()))

    secrets = json.loads(options.secrets.read_text()) if options.secrets else {}
    run_effects(drv_path, drv, secrets=secrets)


def run_all_command(options: EffectsOptions) -> None:
    print("TODO")


def parse_args() -> tuple[Callable[[EffectsOptions], None], EffectsOptions]:
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
        type=str,
        help="Path to the repository",
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
    return args.command, EffectsOptions(secrets=args.secrets)


def main() -> None:
    command, options = parse_args()
    command(options)
