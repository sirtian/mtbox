"""Disk usage helpers shared by the matcher, downloader and dashboard."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DiskUsage:
    total_gb: float
    used_gb: float
    free_gb: float
    percent: float


def disk_usage(path: Path) -> DiskUsage | None:
    try:
        total, used, free = shutil.disk_usage(str(path))
    except OSError:
        return None
    if total <= 0:
        return None
    return DiskUsage(
        total_gb=round(total / (1024**3), 2),
        used_gb=round(used / (1024**3), 2),
        free_gb=round(free / (1024**3), 2),
        percent=round(used * 100.0 / total, 1),
    )
