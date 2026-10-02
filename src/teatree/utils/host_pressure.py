"""Fresh host-scoped pressure feed shared by host hooks and container workers."""

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

from teatree.utils.hook_registry import loop_registry_dir

logger = logging.getLogger(__name__)

MAX_AGE_SECONDS = 60
_missing_warned: set[Path] = set()


@dataclass(frozen=True, slots=True)
class HostPressure:
    cores: int
    load1: float
    ram_available_mib: int
    swap_used_fraction: float | None


def feed_path() -> Path:
    override = os.environ.get("T3_HOST_PRESSURE_PATH")
    return Path(override) if override else loop_registry_dir() / "host-pressure.json"


def reset_missing_warning_memo() -> None:
    """Clear the process-local missing-feed warning memo between tests."""
    _missing_warned.clear()


def _nonnegative_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid(raw: dict, moment: float) -> bool:
    epoch = raw.get("epoch")
    cores = raw.get("cores")
    load = raw.get("load1")
    if not _nonnegative_int(epoch) or not 0 <= moment - epoch < MAX_AGE_SECONDS:
        return False
    if not _nonnegative_int(cores) or cores < 1:
        return False
    if not isinstance(load, (float, int)) or not math.isfinite(load) or load < 0:
        return False
    return all(_nonnegative_int(raw.get(key)) for key in ("ram_available_mib", "swap_used_mib", "swap_total_mib"))


def read_host_pressure(*, path: Path | None = None, now: float | None = None) -> HostPressure | None:
    """Return a validated host reading only when it is younger than 60 seconds."""
    source = path or feed_path()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if source not in _missing_warned:
            logger.warning("host pressure feed missing at %s; using container reading", source)
            _missing_warned.add(source)
        return None
    except (OSError, ValueError):
        logger.warning("host pressure feed unreadable at %s; using container reading", source)
        return None
    moment = time.time() if now is None else now
    if not isinstance(raw, dict):
        logger.warning("host pressure feed malformed at %s; using container reading", source)
        return None
    if not _valid(raw, moment):
        logger.warning("host pressure feed stale or invalid at %s; using container reading", source)
        return None
    cores = raw["cores"]
    load = raw["load1"]
    ram = raw["ram_available_mib"]
    swap_used = raw["swap_used_mib"]
    swap_total = raw["swap_total_mib"]
    fraction = min(1.0, swap_used / swap_total) if swap_total else None
    return HostPressure(cores=cores, load1=float(load), ram_available_mib=ram, swap_used_fraction=fraction)


__all__ = ["MAX_AGE_SECONDS", "HostPressure", "feed_path", "read_host_pressure", "reset_missing_warning_memo"]
