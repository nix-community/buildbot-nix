from pathlib import Path
from buildbot_nix import (
    NixConfigurator,
    BuildbotNixConfig,
)
import getpass
import os
import platform
import subprocess
import multiprocessing
from buildbot.plugins import util
from buildbot.process.factory import BuildFactory
from dataclasses import dataclass


@dataclass
class EvalWorkerConfig:
    count: int
    max_memory_mib: int


@dataclass
class MemoryInfo:
    total_memory_mib: int
    available_memory_mib: int
    zfs_arc_used: int = 0


def get_memory_info_macos() -> MemoryInfo:
    """Get memory information on macOS using vm_stat and sysctl."""
    try:
        # Use vm_stat to get memory information on macOS
        result = subprocess.run(["vm_stat"], capture_output=True, text=True, check=True)

        # Parse vm_stat output
        lines = result.stdout.strip().split("\n")
        page_size = None
        free_pages = 0
        inactive_pages = 0

        for line in lines:
            if "page size of" in line:
                # Extract page size from "Mach Virtual Memory Statistics: (page size of 4096 bytes)"
                page_size = int(line.split("page size of ")[1].split(" bytes")[0])
            elif line.startswith("Pages free:"):
                free_pages = int(line.split(":")[1].strip().rstrip("."))
            elif line.startswith("Pages inactive:"):
                inactive_pages = int(line.split(":")[1].strip().rstrip("."))

        if page_size is None:
            raise ValueError("Could not determine page size from vm_stat")

        # Available memory is free + inactive pages
        available_memory_bytes = (free_pages + inactive_pages) * page_size
        available_memory_mib = available_memory_bytes // (1024 * 1024)

        # Get total memory using sysctl
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=True
        )
        total_memory_bytes = int(result.stdout.strip())
        total_memory_mib = total_memory_bytes // (1024 * 1024)

        return MemoryInfo(total_memory_mib, available_memory_mib)

    except (subprocess.CalledProcessError, ValueError, IndexError) as e:
        print(f"Warning: Could not get accurate memory info on macOS: {e}")
        # Fallback to conservative estimates
        return MemoryInfo(8192, 4096)  # 8GB total, 4GB available


def get_memory_info_linux() -> MemoryInfo:
    """Get memory information on Linux using os.sysconf and ZFS ARC detection."""
    try:
        # Try Linux-specific methods first
        total_pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        available_pages = os.sysconf("SC_AVPHYS_PAGES")

        total_memory_mib = (total_pages * page_size) // (1024 * 1024)
        available_memory_mib = (available_pages * page_size) // (1024 * 1024)

    except (ValueError, OSError):
        # Fallback for other systems
        print(
            "Warning: Could not get memory info via sysconf, using conservative estimates"
        )
        total_memory_mib = 8192  # 8GB default
        available_memory_mib = 4096  # 4GB available

    # Check for ZFS ARC usage on Linux
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

    return MemoryInfo(total_memory_mib, available_memory_mib, zfs_arc_used)


def calculate_eval_workers() -> EvalWorkerConfig:
    """Calculate optimal eval workers based on system resources."""
    # Get system resources
    cpu_count = multiprocessing.cpu_count()

    # Get memory using platform-specific methods
    system = platform.system()

    if system == "Darwin":  # macOS
        memory_info = get_memory_info_macos()
    else:  # Linux and other Unix-like systems
        memory_info = get_memory_info_linux()

    total_memory_mib = memory_info.total_memory_mib
    available_memory_mib = memory_info.available_memory_mib
    zfs_arc_used = memory_info.zfs_arc_used

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
