"""Fresh host-scoped pressure feed shared by host hooks and container workers."""

import json
import logging
import math
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard, cast

from teatree.utils.hook_registry import loop_registry_dir
from teatree.utils.host_os import proc_reads_the_host
from teatree.utils.throttled_log import warn_throttled

logger = logging.getLogger(__name__)

MAX_AGE_SECONDS = 60
PUBLISH_INTERVAL_SECONDS = 15


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


def _feed_validation_fault(raw: object, moment: float) -> str | None:
    if not isinstance(raw, dict) or not _nonnegative_int(raw.get("epoch")):
        return "feed malformed"
    age = moment - raw["epoch"]
    if age < 0:
        return "feed from the future"
    if age >= MAX_AGE_SECONDS:
        return f"feed stale by {age:.0f} seconds"
    return None if _valid(raw, moment) else "feed malformed"


def _load_feed(source: Path, moment: float) -> tuple[HostPressure | None, str | None]:
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "feed missing"
    except OSError:
        return None, "feed unreadable"
    except (ValueError, UnicodeError):
        return None, "feed malformed"
    fault = _feed_validation_fault(raw, moment)
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
    _, fault = _load_feed(path or feed_path(), time.time() if now is None else now)
    return fault


def read_host_pressure(*, path: Path | None = None, now: float | None = None) -> HostPressure | None:
    """Return a validated host reading only when it is younger than 60 seconds."""
    source = path or feed_path()
    reading, fault = _load_feed(source, time.time() if now is None else now)
    if reading is None:
        logger.debug("host pressure %s at %s; using container reading", fault, source)
    return reading


class HostSampler:
    def __init__(self, *, proc_root: Path = Path("/proc"), path: Path | None = None) -> None:
        self.proc_root = proc_root
        self.path = path
        self._previous_swap: tuple[int, int] | None = None

    def _numbers(self, filename: str) -> dict[str, int]:
        rows = (self.proc_root / filename).read_text(encoding="utf-8").splitlines()
        numbers: dict[str, int] = {}
        for row in rows:
            parts = row.split()
            if len(parts) > 1 and parts[1].isdigit():
                numbers[parts[0].rstrip(":")] = int(parts[1])
        return numbers

    def sample(self, *, now: float | None = None) -> dict[str, int | float] | None:
        try:
            meminfo = self._numbers("meminfo")
            available = meminfo["MemAvailable"]
            load1 = float((self.proc_root / "loadavg").read_text(encoding="utf-8").split()[0])
            cores = os.cpu_count()
            if cores is None or cores < 1 or not math.isfinite(load1) or load1 < 0:
                return None
        except (OSError, ValueError, KeyError, IndexError):
            return None
        epoch = int(time.time() if now is None else now)
        reading: dict[str, int | float] = {
            "epoch": epoch,
            "cores": cores,
            "load1": load1,
            "ram_available_mib": available // 1024,
        }
        if "SwapTotal" in meminfo and "SwapFree" in meminfo:
            reading["swap_total_mib"] = meminfo["SwapTotal"] // 1024
            reading["swap_used_mib"] = max(0, meminfo["SwapTotal"] - meminfo["SwapFree"]) // 1024
        try:
            vmstat = self._numbers("vmstat")
            swap_pages = vmstat["pswpin"] + vmstat["pswpout"]
            previous = self._previous_swap
            if previous is not None:
                elapsed = epoch - previous[0]
                if 0 < elapsed <= MAX_AGE_SECONDS and swap_pages >= previous[1]:
                    reading["swap_mib_per_s"] = (
                        (swap_pages - previous[1]) * os.sysconf("SC_PAGE_SIZE") / 1048576 / elapsed
                    )
            self._previous_swap = (epoch, swap_pages)
        except (OSError, KeyError, ValueError):
            self._previous_swap = None
        return reading

    def publish_if_due(self, *, now: float | None = None) -> None:
        if not proc_reads_the_host():
            return
        moment = time.time() if now is None else now
        target = self.path or feed_path()
        current, _ = _load_feed(target, moment)
        if current is not None and moment - current.epoch < PUBLISH_INTERVAL_SECONDS:
            return
        reading = self.sample(now=moment)
        if reading is None:
            warn_throttled(logger, "host-pressure-sample", "host pressure sample unreadable; using container reading")
            return
        temporary: Path | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as file:
                temporary = Path(file.name)
                json.dump(reading, file, separators=(",", ":"))
                file.write("\n")
            temporary.replace(target)
        except OSError:
            warn_throttled(
                logger, "host-pressure-publish", "host pressure feed write failed at %s", target, exc_info=True
            )
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    logger.debug("host pressure temporary file could not be removed at %s", temporary)


__all__ = [
    "MAX_AGE_SECONDS",
    "PUBLISH_INTERVAL_SECONDS",
    "HostPressure",
    "HostSampler",
    "feed_fault",
    "feed_path",
    "read_host_pressure",
]
