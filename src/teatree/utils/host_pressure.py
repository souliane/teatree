"""Fresh host-scoped pressure feed shared by host hooks and container workers."""

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard, cast

from teatree.utils.hook_registry import loop_registry_dir

logger = logging.getLogger(__name__)

MAX_AGE_SECONDS = 60


@dataclass(frozen=True, slots=True)
class HostPressure:
    epoch: int
    cores: int
    load1: float
    ram_available_mib: int
    swap_mib_per_s: float | None
    vm_pressure_level: int | None


def feed_path() -> Path:
    override = os.environ.get("T3_HOST_PRESSURE_PATH")
    return Path(override) if override else loop_registry_dir() / "host-pressure.json"


def _nonnegative_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and _nonnegative_number(value)


def _valid(raw: object, moment: float) -> str | None:
    if not isinstance(raw, dict):
        return "feed malformed"
    epoch = raw.get("epoch")
    cores = raw.get("cores")
    load = raw.get("load1")
    if not _nonnegative_int(epoch):
        return "feed malformed"
    if epoch > moment:
        return "feed from the future"
    age = moment - epoch
    if age >= MAX_AGE_SECONDS:
        return f"feed stale by {age:.0f} seconds"
    if not _nonnegative_int(cores) or cores < 1 or not _nonnegative_number(load):
        return "feed malformed"
    return None if _nonnegative_int(raw.get("ram_available_mib")) else "feed malformed"


def _nonnegative_number(value: object) -> TypeGuard[float | int]:
    try:
        return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _optional_rate(value: object) -> float | None:
    return float(value) if _nonnegative_number(value) else None


def _optional_level(value: object) -> int | None:
    return value if _nonnegative_int(value) else None


def load_feed(source: Path, moment: float) -> tuple[HostPressure | None, str | None]:
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "feed missing"
    except OSError:
        return None, "feed unreadable"
    except (ValueError, UnicodeError):
        return None, "feed malformed"
    fault = _valid(raw, moment)
    if fault is not None:
        return None, fault
    return (
        HostPressure(
            epoch=cast("int", raw["epoch"]),
            cores=cast("int", raw["cores"]),
            load1=float(cast("float | int", raw["load1"])),
            ram_available_mib=cast("int", raw["ram_available_mib"]),
            swap_mib_per_s=_optional_rate(raw.get("swap_mib_per_s")),
            vm_pressure_level=_optional_level(raw.get("vm_pressure_level")),
        ),
        None,
    )


def feed_fault(*, path: Path | None = None, now: float | None = None) -> str | None:
    _, fault = load_feed(path or feed_path(), time.time() if now is None else now)
    return fault


def read_host_pressure(*, path: Path | None = None, now: float | None = None) -> HostPressure | None:
    """Return a validated host reading only when it is younger than 60 seconds."""
    source = path or feed_path()
    reading, fault = load_feed(source, time.time() if now is None else now)
    if reading is None:
        logger.debug("host pressure %s at %s; using container reading", fault, source)
    return reading


__all__ = [
    "MAX_AGE_SECONDS",
    "HostPressure",
    "feed_fault",
    "feed_path",
    "load_feed",
    "read_host_pressure",
]
