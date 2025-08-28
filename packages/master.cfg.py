from pathlib import Path
from buildbot_nix import (
    NixConfigurator,
    BuildbotNixConfig,
)
import getpass
import os
import multiprocessing
from buildbot.plugins import util
from buildbot.process.factory import BuildFactory
from dataclasses import dataclass


@dataclass
class EvalWorkerConfig:
    count: int
    max_memory_mib: int


def calculate_eval_workers() -> EvalWorkerConfig:
    """Calculate optimal eval workers based on system resources."""
    # Get system resources
    cpu_count = multiprocessing.cpu_count()

    # Get memory using os.sysconf()
    total_pages = os.sysconf("SC_PHYS_PAGES")
    page_size = os.sysconf("SC_PAGE_SIZE")
    available_pages = os.sysconf("SC_AVPHYS_PAGES")

    total_memory_mib = (total_pages * page_size) // (1024 * 1024)
    available_memory_mib = (available_pages * page_size) // (1024 * 1024)

    # Check for ZFS ARC usage
    zfs_arc_used = 0
    try:
        with open("/proc/spl/kstat/zfs/arcstats", "r") as f:
            for line in f:
                if line.startswith("size"):
                    # Format: "size 4 <value>"
                    parts = line.split()
                    if len(parts) >= 3:
                        zfs_arc_used = int(parts[2]) // (1024 * 1024)  # Convert to MiB
                        break
    except (FileNotFoundError, PermissionError, ValueError):
        # Not a ZFS system or can't read ARC stats
        pass

    # If ZFS is present, account for ARC cache which can be reclaimed
    effective_available_memory = available_memory_mib
    if zfs_arc_used > 0:
        # ARC can shrink but keep some minimum (25% of current size)
        reclaimable_arc = int(zfs_arc_used * 0.75)
        effective_available_memory += reclaimable_arc
        print(
            f"ZFS ARC detected: {zfs_arc_used}MiB (can reclaim ~{reclaimable_arc}MiB)"
        )

    # Account for memory spike before limit check
    # Since memory is checked after eval, we need headroom for one full eval
    # Assume worst case: one eval can use up to the limit before being killed
    spike_headroom = 2048  # 2GB for eval spike

    # Heuristic for worker count
    # Reserve more memory: 2GB for system/buildbot + spike headroom
    memory_for_workers = max(2048, effective_available_memory - 2048 - spike_headroom)

    # Default 2GB per worker, but can go down to 1GB if needed
    eval_max_memory = 2048
    memory_based_workers = max(1, memory_for_workers // eval_max_memory)

    # CPU-based limit: typically fewer workers than cores for eval
    cpu_based_workers = max(1, min(cpu_count, (cpu_count + 1) // 2))

    # Take minimum and cap at reasonable max
    optimal_workers = max(1, min(memory_based_workers, cpu_based_workers, 16))

    # If memory-limited, try to fit more workers with less memory each
    if memory_based_workers < cpu_based_workers:
        min_memory = 1024  # 1GB minimum
        possible_workers = min(cpu_based_workers, memory_for_workers // min_memory)
        if possible_workers > optimal_workers:
            eval_max_memory = max(min_memory, memory_for_workers // possible_workers)
            optimal_workers = possible_workers

    print(
        f"System: {cpu_count} CPUs, {total_memory_mib}MiB RAM ({available_memory_mib}MiB available)"
    )
    if zfs_arc_used > 0:
        print(
            f"Effective available memory (with ZFS ARC): {effective_available_memory}MiB"
        )
    print(f"Using {optimal_workers} eval workers with {eval_max_memory}MiB each")
    print(f"Reserved {spike_headroom}MiB for eval spikes before memory checks")

    return EvalWorkerConfig(optimal_workers, eval_max_memory)


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

# Calculate optimal eval workers dynamically
eval_config = calculate_eval_workers()

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
    eval_max_memory_size=eval_config.max_memory_mib,
    eval_worker_count=eval_config.count,
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
