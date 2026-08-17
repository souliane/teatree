"""Which MOUNT POINT a path sits on — the boundary ``rename(2)`` refuses to cross.

``rename(2)`` returns ``EXDEV`` between distinct mount points, not merely between
distinct devices: two bind mounts of one filesystem report the same ``st_dev``
and still cannot be renamed across. A ``st_dev`` comparison — the obvious way to
guard a move — therefore concludes it is safe and the move fails anyway, which is
how ``workspace relocate`` came to prescribe a move it could never complete
(#4368). Everything here keys on the mount table; ``st_dev`` is never consulted.

The table is ``/proc/self/mountinfo``, so an answer exists only where that does. A
venue that cannot read it gets ``None`` — unknown, never a fabricated "same mount".
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_MOUNTINFO = Path("/proc/self/mountinfo")
_MOUNT_POINT_FIELD = 4
_OCTAL_ESCAPE = re.compile(r"\\(\d{3})")


@dataclass(frozen=True)
class MountEntry:
    """One ``/proc/self/mountinfo`` row: where it is mounted, and what it carries."""

    mount_point: Path
    fstype: str


def parse_mountinfo(text: str) -> tuple[MountEntry, ...]:
    """Every mount in *text*, in table order — a later row over-mounts an earlier one."""
    entries = []
    for line in text.splitlines():
        head, separator, tail = line.partition(" - ")
        fields, tail_fields = head.split(), tail.split()
        if not separator or len(fields) <= _MOUNT_POINT_FIELD or not tail_fields:
            continue
        entries.append(MountEntry(mount_point=Path(_unescape(fields[_MOUNT_POINT_FIELD])), fstype=tail_fields[0]))
    return tuple(entries)


def _unescape(field: str) -> str:
    # mountinfo octal-escapes space, tab, newline and backslash in its path fields.
    return _OCTAL_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), field)


def read_mount_entries() -> tuple[MountEntry, ...] | None:
    """This venue's mount table, or ``None`` when it cannot be read (non-Linux, sandboxed)."""
    try:
        return parse_mountinfo(_MOUNTINFO.read_text(encoding="utf-8"))
    except OSError:
        return None


def mount_entry_for(path: Path, entries: Sequence[MountEntry]) -> MountEntry | None:
    """The longest mount point covering *path* — the mount it actually sits on.

    Ties go to the later row, which is what over-mounting means: two mounts on one
    point are resolved by whichever was applied last.
    """
    best: MountEntry | None = None
    for entry in entries:
        if _covers(entry.mount_point, path) and (best is None or _at_least_as_deep(entry, best)):
            best = entry
    return best


def _at_least_as_deep(entry: MountEntry, best: MountEntry) -> bool:
    return len(entry.mount_point.parts) >= len(best.mount_point.parts)


def _covers(mount_point: Path, path: Path) -> bool:
    return path == mount_point or mount_point in path.parents


def mount_point_for(path: Path) -> Path | None:
    """The mount point *path* sits on, or ``None`` when it cannot be established.

    *path* need not exist: its nearest existing ancestor decides, because that is
    the mount a rename into *path* would land on.
    """
    entries = read_mount_entries()
    if entries is None:
        return None
    entry = mount_entry_for(_nearest_existing(path), entries)
    return entry.mount_point if entry is not None else None


def mount_boundary_between(src: Path, dst: Path) -> tuple[Path, Path] | None:
    """The two mount points when a rename from *src* to *dst* would return ``EXDEV``.

    ``None`` covers both "they share a mount point" and "this venue could not read
    the table" — collapsed because the correct action is identical for each: go
    ahead, and let the rename itself speak. A boundary is only ever reported when
    one was PROVED, never inferred from silence.
    """
    src_mount, dst_mount = mount_point_for(src), mount_point_for(dst)
    if src_mount is None or dst_mount is None or src_mount == dst_mount:
        return None
    return src_mount, dst_mount


def _nearest_existing(path: Path) -> Path:
    """*path* resolved, walking up to the first ancestor that exists.

    A move's destination does not exist yet, and ``resolve()`` cannot follow
    symlinks that are not there — the ancestor that does exist is the one whose
    mount decides. ``parents`` ends at the filesystem root, so something always
    exists and the search needs no fallback.
    """
    absolute = path if path.is_absolute() else Path.cwd() / path
    return next(candidate for candidate in (absolute, *absolute.parents) if candidate.exists()).resolve()


__all__ = [
    "MountEntry",
    "mount_boundary_between",
    "mount_entry_for",
    "mount_point_for",
    "parse_mountinfo",
    "read_mount_entries",
]
