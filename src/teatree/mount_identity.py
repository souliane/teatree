"""Where a path physically lives, spelled identically by every mount namespace that reaches it (#4923).

The host and the container can spell one path the same while it names two different
directories, so "absent here" proves nothing about the other venue. The mount table maps
a path to its filesystem device and the path inside that filesystem: two venues looking at
the same directory compute the same token, and two looking at different ones do not.
"""

import os
import re
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
    target = os.path.realpath(path)
    mount = _covering_mount(table, target)
    if mount is None:
        return None
    device, root, point = mount
    inside = os.path.normpath(PurePosixPath(root) / os.path.relpath(target, point))
    if not device.startswith("0:"):
        return f"{device}:{inside}"
    boot = boot_id if boot_id is not None else _read(_BOOT_ID)
    return f"{device}@{boot.strip()}:{inside}" if boot else None


def _covering_mount(table: str, target: str) -> tuple[str, str, str] | None:
    """The deepest mount holding *target*; of mounts stacked on one point, the last listed is on top."""
    covering: tuple[str, str, str] | None = None
    for line in table.splitlines():
        fields = line.split()
        if len(fields) < _MOUNT_POINT_FIELDS:
            continue
        device, root, point = fields[2], _unescape(fields[3]), _unescape(fields[4])
        if _holds(point, target) and (covering is None or len(point) >= len(covering[2])):
            covering = (device, root, point)
    return covering


def _holds(point: str, target: str) -> bool:
    return point in {"/", target} or target.startswith(f"{point}/")


def _unescape(field: str) -> str:
    return _OCTAL_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), field)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


__all__ = ["physical_location"]
