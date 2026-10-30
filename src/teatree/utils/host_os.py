"""Whether this process runs inside a container."""

from pathlib import Path

CONTAINER_MARKERS = (Path("/.dockerenv"), Path("/run/.containerenv"))


def running_in_a_container(markers: tuple[Path, ...] = CONTAINER_MARKERS) -> bool:
    return any(marker.exists() for marker in markers)
