"""Service entry point.

Loads configuration, sets up structured logging, and runs the asyncio
service loop. Components (database, web frontend, orchestrator, forge
clients) are started here as they are implemented.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .bootstrap import run_service
from .config import Config
from .log import setup_logging

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="nixbot",
        description="nixbot CI service",
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="path to the configuration file (JSON)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
    )
    parser.add_argument(
        "--log-format",
        default="json",
        choices=["json", "text"],
    )
    return parser.parse_args(argv)


async def run(config: Config) -> None:
    """Run the service until a termination signal arrives.

    Signal handling lives in run_service: uvicorn installs its own
    SIGINT/SIGTERM handlers (overwriting anything set here), and the
    bootstrap treats the first server exit as shutdown.
    """
    logger.info(
        "nixbot starting",
        extra={"url": config.url, "state_dir": str(config.state_dir)},
    )
    await run_service(config)
    logger.info("nixbot shutting down")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    setup_logging(args.log_level, json_format=args.log_format == "json")
    config = Config.load(args.config)
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
