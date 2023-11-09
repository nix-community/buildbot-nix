#!/usr/bin/env python3

import multiprocessing
import os
import socket
from dataclasses import dataclass
from pathlib import Path

from buildbot_worker.bot import Worker
from twisted.application import service


def require_env(key: str) -> str:
    val = os.environ.get(key)
    assert val is not None, "val is not set"
    return val


@dataclass
class WorkerConfig:
    password: str = Path(require_env("WORKER_PASSWORD_FILE")).read_text().rstrip("\r\n")
    worker_count: int = int(
        os.environ.get("WORKER_COUNT", str(multiprocessing.cpu_count()))
    )
    buildbot_dir: str = require_env("BUILDBOT_DIR")
    master_url: str = require_env("MASTER_URL")


def setup_worker(
    application: service.Application, id: int, config: WorkerConfig
) -> None:
    basedir = f"{config.buildbot_dir}-{id:03}"
    os.makedirs(basedir, mode=0o700, exist_ok=True)

    hostname = socket.gethostname()
    workername = f"{hostname}-{id:03}"
    keepalive = 600
    umask = None
    maxdelay = 300
    numcpus = None
    allow_shutdown = None

    s = Worker(
        None,
        None,
        workername,
        config.password,
        basedir,
        keepalive,
        connection_string=config.master_url,
        umask=umask,
        maxdelay=maxdelay,
        numcpus=numcpus,
        allow_shutdown=allow_shutdown,
    )
    # defaults to 4096, bump to 10MB for nix-eval-jobs
    s.bot.max_line_length = 10485760
    s.setServiceParent(application)


def setup_workers(application: service.Application, config: WorkerConfig) -> None:
    for i in range(config.worker_count):
        setup_worker(application, i, config)


# note: this line is matched against to check that this is a worker
# directory; do not edit it.
application = service.Application("buildbot-worker")
setup_workers(application, WorkerConfig())
