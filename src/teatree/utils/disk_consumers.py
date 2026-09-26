"""Bounded, read-only contributor sizes for a root-disk pressure alarm.

Each external probe has a short deadline. An unavailable size is labelled unknown;
the doctor must still report the full-disk condition rather than waiting on ``du``
or Docker when the box is already under pressure.
"""

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from teatree.utils.run import SUBPROCESS_UNREACHABLE, run_allowed_to_fail

_GIB = 1024**3
_DU_DEADLINE = 4
_DOCKER_DEADLINE = 3
_SIZE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?B)$", re.IGNORECASE)
_UNIT = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}


def _bytes_from_docker_size(raw: str) -> int | None:
    match = _SIZE.fullmatch(raw.strip())
    if match is None:
        return None
    return int(float(match.group(1)) * _UNIT[match.group(2).upper()])


def _docker_build_cache_bytes() -> int | None:
    docker = shutil.which("docker")
    if docker is None:
        return None
    try:
        result = run_allowed_to_fail(
            [docker, "system", "df", "--format", "{{json .}}"],
            timeout=_DOCKER_DEADLINE,
            expected_codes=None,
        )
    except SUBPROCESS_UNREACHABLE:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and str(row.get("Type", "")).lower() == "build cache":
            return _bytes_from_docker_size(str(row.get("Size", "")))
    return None


def _worktree_bytes() -> int | None:
    try:
        from teatree.config.loader import worktree_root  # noqa: PLC0415 — config at call time
        from teatree.core.worktree.worktree_roots import registered_worktree_roots  # noqa: PLC0415 — ORM at call time

        roots = {worktree_root(), *registered_worktree_roots()}
    except Exception:  # noqa: BLE001 — optional contributor must never mask the critical alarm
        return None
    # Keep the outermost roots; counting a child beside its parent double-counts bytes.
    selected: list[Path] = []
    for path in sorted((root.resolve() for root in roots if root.is_dir()), key=lambda item: len(item.parts)):
        if not any(path == parent or parent in path.parents for parent in selected):
            selected.append(path)
    if not selected:
        return 0
    du = shutil.which("du")
    if du is None:
        return None
    try:
        result = run_allowed_to_fail(
            [du, "-sk", *map(str, selected)],
            timeout=_DU_DEADLINE,
            expected_codes=None,
        )
    except SUBPROCESS_UNREACHABLE:
        return None
    if result.returncode != 0:
        return None
    sizes = []
    for line in result.stdout.splitlines():
        first = line.split(maxsplit=1)[0]
        if first.isdigit():
            sizes.append(int(first) * 1024)
    return sum(sizes) if len(sizes) == len(selected) else None


def _control_db_bytes() -> int | None:
    try:
        from django.conf import settings  # noqa: PLC0415 — call after doctor bootstraps Django

        db = Path(str(settings.DATABASES["default"]["NAME"]))
        return sum(path.stat().st_size for path in (db, Path(f"{db}-wal")) if path.is_file())
    except (KeyError, OSError, TypeError, ValueError):
        return None


def _display(value: int | None) -> str:
    return f"{value / _GIB:.1f} GiB" if value is not None else "unknown"


def _safe_size(probe: Callable[[], int | None]) -> int | None:
    try:
        return probe()
    except Exception:  # noqa: BLE001 — no contributor may mask the critical disk alarm
        return None


def summary() -> str:
    """The three reclaim-relevant categories, largest measured contributor first."""
    measurements = [
        ("Docker build cache", _safe_size(_docker_build_cache_bytes)),
        ("worktrees/env dirs", _safe_size(_worktree_bytes)),
        ("control DB", _safe_size(_control_db_bytes)),
    ]
    measurements.sort(key=lambda pair: pair[1] if pair[1] is not None else -1, reverse=True)
    return "; ".join(f"{name} {_display(size)}" for name, size in measurements)


__all__ = ["summary"]
