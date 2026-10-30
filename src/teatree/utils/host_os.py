"""Whether a Linux procfs reading describes the declared host."""

import os
import platform
from pathlib import Path

CONTAINER_MARKERS = (Path("/.dockerenv"), Path("/run/.containerenv"))


def running_in_a_container(markers: tuple[Path, ...] = CONTAINER_MARKERS) -> bool:
    return any(marker.exists() for marker in markers)


def proc_reads_the_host(markers: tuple[Path, ...] = CONTAINER_MARKERS) -> bool:
    declared = os.environ.get("TEATREE_HOST_OS", "").strip()
    if declared:
        return declared == "Linux"
    return platform.system() == "Linux" and not running_in_a_container(markers)
