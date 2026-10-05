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
    swap_mib_per_s: float | None
    vm_pressure_level: int | None


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
    if not _nonnegative_number(load):
        return False
    return _nonnegative_int(raw.get("ram_available_mib"))


def _nonnegative_number(value: object) -> TypeGuard[float | int]:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _optional_rate(value: object) -> float | None:
    return float(value) if _nonnegative_number(value) else None


def _optional_level(value: object) -> int | None:
    return value if _nonnegative_int(value) else None


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
    return HostPressure(
        cores=raw["cores"],
        load1=float(raw["load1"]),
        ram_available_mib=raw["ram_available_mib"],
        swap_mib_per_s=_optional_rate(raw.get("swap_mib_per_s")),
        vm_pressure_level=_optional_level(raw.get("vm_pressure_level")),
    )


__all__ = ["MAX_AGE_SECONDS", "HostPressure", "feed_path", "read_host_pressure", "reset_missing_warning_memo"]
