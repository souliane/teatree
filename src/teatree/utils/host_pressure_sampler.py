"""Linux producer for the host pressure feed: sample ``/proc`` and publish the file readers validate."""

import json
import logging
import math
import os
import tempfile
import time
from contextlib import suppress
from pathlib import Path

from teatree.utils.host_pressure import MAX_AGE_SECONDS, feed_path, load_feed
from teatree.utils.throttled_log import warn_throttled

logger = logging.getLogger(__name__)

PUBLISH_INTERVAL_SECONDS = 15


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
        moment = time.time() if now is None else now
        target = self.path or feed_path()
        current, _ = load_feed(target, moment)
        if current is not None and moment - current.epoch < PUBLISH_INTERVAL_SECONDS:
            return
        reading = self.sample(now=moment)
        if reading is None:
            warn_throttled(logger, "host-pressure-sample", "host pressure sample unreadable; using container reading")
            return
        temporary: Path | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as file:
                temporary = Path(file.name)
                json.dump(reading, file, separators=(",", ":"))
                file.write("\n")
            temporary.chmod(0o644)
            temporary.replace(target)
        except OSError:
            warn_throttled(
                logger, "host-pressure-publish", "host pressure feed write failed at %s", target, exc_info=True
            )
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)


__all__ = ["PUBLISH_INTERVAL_SECONDS", "HostSampler"]
