"""Where a path physically lives, spelled identically by every mount namespace that reaches it (#4923).

The host and the container can spell one path the same while it names two different
directories, so "absent here" proves nothing about the other venue. The mount table maps
a path to its filesystem device and the path inside that filesystem: two venues looking at
the same directory compute the same token, and two looking at different ones do not.
"""

import os
import re
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath

_MOUNTINFO = Path("/proc/self/mountinfo")
_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")
#: A mountinfo line names the device, the root inside it and the mount point in its first five fields.
_MOUNT_POINT_FIELDS = 5


def physical_location(path: Path, *, mountinfo: str | None = None, boot_id: str | None = None) -> str | None:
    """``<major:minor>:<path inside the filesystem>`` for *path*, or ``None`` when no mount table is readable.

    An anonymous device (major 0: overlay, tmpfs) is renumbered on every boot, so its token carries the boot id.
    """
    table = mountinfo if mountinfo is not None else _read(_MOUNTINFO)
    if not table:
        return None
    location = parsed_mount_table(table).locate(path)
    if location is None:
        return None
    device, inside = location
    if not device.startswith("0:"):
        return f"{device}:{inside}"
    boot = boot_id if boot_id is not None else _read(_BOOT_ID)
    return f"{device}@{boot.strip()}:{inside}" if boot else None


def read_mount_table() -> str:
    return _read(_MOUNTINFO) or ""


def mount_spellings(path: str | Path, *, mountinfo: str | None = None) -> list[str]:
    table = mountinfo if mountinfo is not None else read_mount_table()
    return parsed_mount_table(table).spellings(path)


@dataclass(frozen=True, slots=True)
class _Mount:
    mount_id: str
    parent_id: str
    device: str
    root: str
    point: str


class MountTable:
    def __init__(self, text: str) -> None:
        self.mounts = _mounts(text)
        self.by_device: dict[str, list[_Mount]] = defaultdict(list)
        for mount in self.mounts:
            self.by_device[mount.device].append(mount)
        ids = {mount.mount_id for mount in self.mounts}
        self.root = next(
            (
                mount
                for mount in self.mounts
                if mount.point == "/" and (mount.parent_id == mount.mount_id or mount.parent_id not in ids)
            ),
            None,
        )
        self.children = {
            (mount.point, mount.parent_id): mount for mount in self.mounts if mount.mount_id != mount.parent_id
        }

    def locate(self, path: str | Path, *, resolved: str | None = None) -> tuple[str, str] | None:
        target = resolved if resolved is not None else os.path.realpath(path)
        mount = self._covering_mount(target)
        if mount is None:
            return None
        return mount.device, os.path.normpath(str(PurePosixPath(mount.root) / os.path.relpath(target, mount.point)))

    def spellings(self, path: str | Path, *, resolved: str | None = None) -> list[str]:
        location = self.locate(path, resolved=resolved)
        if location is None:
            return []
        device, inside = location
        spellings: list[str] = []
        for mount in self.by_device[device]:
            if not _holds(mount.root, inside):
                continue
            spelling = os.path.normpath(str(PurePosixPath(mount.point) / os.path.relpath(inside, mount.root)))
            if self.locate(spelling) == location:
                spellings.append(spelling)
        return list(dict.fromkeys(spellings))

    def _covering_mount(self, target: str) -> _Mount | None:
        current = self.root
        if current is None:
            ids = {mount.mount_id for mount in self.mounts}
            visible = [mount for mount in self.mounts if mount.parent_id not in ids and _holds(mount.point, target)]
            if len(visible) != 1:
                return None
            current = visible[0]
        point = "/"
        visited = {current.mount_id}
        for part in PurePosixPath(target).parts:
            if part != "/":
                point = f"{point.rstrip('/')}/{part}"
            while (following := self.children.get((point, current.mount_id))) is not None:
                if following.mount_id in visited:
                    break
                current = following
                visited.add(current.mount_id)
        return current


@lru_cache(maxsize=2)
def parsed_mount_table(text: str) -> MountTable:
    return MountTable(text)


def _mounts(table: str) -> list[_Mount]:
    mounts: list[_Mount] = []
    for line in table.splitlines():
        fields = line.split()
        if len(fields) >= _MOUNT_POINT_FIELDS:
            mounts.append(_Mount(fields[0], fields[1], fields[2], _unescape(fields[3]), _unescape(fields[4])))
    return mounts


def _holds(point: str, target: str) -> bool:
    return point in {"/", target} or target.startswith(f"{point}/")


def _unescape(field: str) -> str:
    return _OCTAL_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), field)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return None


__all__ = ["MountTable", "mount_spellings", "parsed_mount_table", "physical_location", "read_mount_table"]
