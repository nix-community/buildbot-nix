from pathlib import Path
from buildbot_nix import (
    NixConfigurator,
    BuildbotNixConfig,
)
import getpass
from buildbot.plugins import util
from buildbot.process.factory import BuildFactory

factory = BuildFactory()

STATE_DIR = Path(".")
PORT = 8012
url = f"http://localhost:{PORT}"
current_user = getpass.getuser()
gcroots_dir = Path("/nix/var/nix/gcroots/per-user/") / current_user
if not gcroots_dir.exists():
    raise RuntimeError(
        f"GC roots directory {gcroots_dir} does not exist. "
        "Please ensure that the Nix daemon is running and that you have the correct permissions."
        f" You can create it with `sudo mkdir -p {gcroots_dir}` and `sudo chown {current_user} {gcroots_dir}`."
    )

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
    webhook_base_url=url,
    url=url,
    gcroots_dir=gcroots_dir,
    gcroots_user=getpass.getuser(),
    admins=["admin"],
)

c = BuildmasterConfig = dict(
    title="Hello World CI",
    titleURL="https://buildbot.github.io/hello-world/",
    buildbotURL=url,
    configurators=[
        NixConfigurator(buildbot_nix_config),
    ],
    protocols={"pb": {"port": "tcp:9989:interface=\\:\\:1"}},
    www=dict(port=PORT, plugins=dict(), auth=util.UserPasswordAuth({"admin": "admin"})),
)
