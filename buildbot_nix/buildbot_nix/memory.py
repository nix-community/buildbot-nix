"""Eval worker count and memory-limit sizing.

Only the Linux path is needed, the service is Linux-only.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_EVAL_MAX_MEMORY_MIB = 2048
MIN_EVAL_MEMORY_MIB = 1024
# Headroom for one eval's memory spike before the limit check triggers.
SPIKE_HEADROOM_MIB = 2048
MAX_EVAL_WORKERS = 16


@dataclass
class EvalWorkerConfig:
    count: int
    max_memory_mib: int


@dataclass
class MemoryInfo:
    total_memory_mib: int
    available_memory_mib: int
    zfs_arc_used: int = 0


def get_memory_info(
    arcstats_path: Path = Path("/proc/spl/kstat/zfs/arcstats"),
) -> MemoryInfo:
    """Get memory information on Linux, including reclaimable ZFS ARC."""
    try:
        total_pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        available_pages = os.sysconf("SC_AVPHYS_PAGES")
        total_memory_mib = (total_pages * page_size) // (1024 * 1024)
        available_memory_mib = (available_pages * page_size) // (1024 * 1024)
    except (ValueError, OSError):
        logger.warning(
            "could not get memory info via sysconf, using conservative estimates"
        )
        total_memory_mib = 8192
        available_memory_mib = 4096

    zfs_arc_used = 0
    try:
        with arcstats_path.open() as f:
            for line in f:
                if line.startswith("size"):
                    # Format: "size 4 <value>"
                    parts = line.split()
                    if len(parts) >= 3:  # noqa: PLR2004
                        zfs_arc_used = int(parts[2]) // (1024 * 1024)
                        break
    except (FileNotFoundError, PermissionError, ValueError):
        pass  # Not a ZFS system or ARC stats unreadable.

    return MemoryInfo(total_memory_mib, available_memory_mib, zfs_arc_used)


def calculate_eval_workers(
    memory_info: MemoryInfo | None = None,
    cpu_count: int | None = None,
) -> EvalWorkerConfig:
    """Calculate optimal eval workers based on system resources."""
    if cpu_count is None:
        cpu_count = multiprocessing.cpu_count()
    if memory_info is None:
        memory_info = get_memory_info()

    # ZFS ARC can shrink under pressure; treat 75% of it as reclaimable.
    effective_available_memory = memory_info.available_memory_mib
    if memory_info.zfs_arc_used > 0:
        reclaimable_arc = int(memory_info.zfs_arc_used * 0.75)
        effective_available_memory += reclaimable_arc

    # Reserve 2GB for system/service plus headroom for one eval spike,
    # since the worker memory limit is checked after an eval finishes.
    memory_for_workers = max(
        2048, effective_available_memory - 2048 - SPIKE_HEADROOM_MIB
    )

    eval_max_memory = DEFAULT_EVAL_MAX_MEMORY_MIB
    memory_based_workers = max(1, memory_for_workers // eval_max_memory)

    # Eval is memory-bound: typically fewer workers than cores.
    cpu_based_workers = max(1, min(cpu_count, (cpu_count + 1) // 2))

    optimal_workers = max(
        1, min(memory_based_workers, cpu_based_workers, MAX_EVAL_WORKERS)
    )

    # If memory-limited, try to fit more workers with less memory each.
    if memory_based_workers < cpu_based_workers:
        possible_workers = min(
            cpu_based_workers, memory_for_workers // MIN_EVAL_MEMORY_MIB
        )
        if possible_workers > optimal_workers:
            eval_max_memory = max(
                MIN_EVAL_MEMORY_MIB, memory_for_workers // possible_workers
            )
            optimal_workers = possible_workers

    return EvalWorkerConfig(int(optimal_workers), int(eval_max_memory))
