from pathlib import Path
from buildbot_nix import (
    NixConfigurator,
    BuildbotNixConfig,
)
from buildbot.process.factory import BuildFactory

factory = BuildFactory()

STATE_DIR = Path(".")
PORT = 8012

buildbot_nix_config = BuildbotNixConfig(
    db_url="sqlite:///state.sqlite",
    pull_based=dict(
        repositories={
            "buildbot-nix": dict(
                name="buildbot-nix",
                url="https://github.com/nix-community/buildbot-nix",
                default_branch="main",
            )
        },
        poll_spread=None,
    ),
    build_systems=["x86_64-linux"],
    eval_max_memory_size=4096,
    eval_worker_count=4,
    local_workers=4,
    domain="localhost",
    webhook_base_url=f"http://localhost:{PORT}",
    url=f"http://localhost:{PORT}",
)

c = BuildmasterConfig = dict(
    title="Hello World CI",
    titleURL="https://buildbot.github.io/hello-world/",
    configurators=[
        NixConfigurator(buildbot_nix_config),
    ],
    protocols={"pb": {"port": "tcp:9989:interface=\\:\\:1"}},
    www=dict(port=PORT, plugins=dict()),
)
