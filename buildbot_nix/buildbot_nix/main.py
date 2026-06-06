"""Engine entry point.

Loads configuration, sets up structured logging, and runs the asyncio
service loop. Components (database, web frontend, orchestrator, forge
clients) are started here as they are implemented.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from .config import EngineConfig
from .log import setup_logging
from .service import run_service

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="buildbot-nix",
        description="buildbot-nix CI engine",
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="path to the engine configuration file (JSON)",
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


async def run(config: EngineConfig) -> None:
    """Run the engine until a termination signal arrives."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    logger.info(
        "buildbot-nix engine starting",
        extra={"url": config.url, "state_dir": str(config.state_dir)},
    )
    serve = asyncio.create_task(run_service(config))
    stop_wait = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait(
        {serve, stop_wait}, return_when=asyncio.FIRST_COMPLETED
    )
    if serve in done:
        serve.result()  # propagate startup/serve errors
    serve.cancel()
    logger.info("buildbot-nix engine shutting down")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    setup_logging(args.log_level, json_format=args.log_format == "json")
    config = EngineConfig.load(args.config)
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
