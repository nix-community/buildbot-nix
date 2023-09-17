#!/usr/bin/env python3

import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from buildbot.plugins import schedulers, util

# allow to import modules
sys.path.append(str(Path(__file__).parent))

from buildbot_nix import GithubConfig, NixConfigurator  # noqa: E402


def build_config() -> dict[str, Any]:
    c: dict[str, Any] = {}
    c["buildbotNetUsageData"] = None
    # configure a janitor which will delete all logs older than one month, and will run on sundays at noon
    c["configurators"] = [
        util.JanitorConfigurator(logHorizon=timedelta(weeks=4), hour=12, dayOfWeek=6),
        NixConfigurator(
            github=GithubConfig(
                oauth_id=os.environ["GITHUB_OAUTH_ID"],
                admins=os.environ.get("GITHUB_ADMINS", "").split(" "),
                buildbot_user=os.environ["BUILDBOT_GITHUB_USER"],
            ),
            nix_eval_max_memory_size=int(
                os.environ.get("NIX_EVAL_MAX_MEMORY_SIZE", "4096")
            ),
            nix_supported_systems=os.environ.get("NIX_SUPPORTED_SYSTEMS", "auto").split(
                " "
            ),
        ),
    ]
    c["schedulers"] = [
        schedulers.SingleBranchScheduler(
            name="nixpkgs",
            change_filter=util.ChangeFilter(
                repository_re=r"https://github\.com/.*/nixpkgs",
                filter_fn=lambda c: c.branch
                == c.properties.getProperty("github.repository.default_branch"),
            ),
            treeStableTimer=20,
            builderNames=["Mic92/dotfiles/update-flake"],
        ),
    ]
    c["builders"] = []
    c["projects"] = []
    c["workers"] = []
    c["services"] = []
    c["www"] = {
        "plugins": dict(
            base_react={}, waterfall_view={}, console_view={}, grid_view={}
        ),
        "port": int(os.environ.get("PORT", "1810")),
    }

    c["db"] = {"db_url": os.environ.get("DB_URL", "sqlite:///state.sqlite")}
    c["protocols"] = {"pb": {"port": "tcp:9989:interface=\\:\\:"}}
    c["buildbotURL"] = os.environ["BUILDBOT_URL"]

    return c


BuildmasterConfig = build_config()
