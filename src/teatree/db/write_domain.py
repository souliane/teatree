"""Where the control DB lives, and who currently holds it open for writing.

The claim-file check this replaces asserted that a container had once written a
marker beside the database. That is a statement about the past, and it was GREEN
throughout five corruptions in one day, because the thing it could not see is the
thing that does the damage: a process that opened the file read-write BEFORE the
claim existed keeps that descriptor for its whole life — ``read_write_allowed`` is
evaluated once at connection setup and has no revocation path.

So this module observes two facts that are true NOW:

* the database's directory is the control-DB volume, not a host bind mount — a file
    with no host path cannot be opened by a host process at all; and
* which processes hold a READ-WRITE descriptor on it, read from ``/proc``.
"""

import os
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from teatree.docker.workflow import is_running_in_container
from teatree.paths import control_db_dir
from teatree.utils.mount_points import mount_entry_for, read_mount_entries

_PROC = Path("/proc")
_O_ACCMODE = 0o3
_O_RDONLY = 0o0
_LOCAL_FSTYPES = frozenset({"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "tmpfs", "overlay"})

_FDINFO_FLAGS = re.compile(r"^flags:\s*(\d+)$", re.MULTILINE)


class DescriptorTableUnavailableError(RuntimeError):
    """The descriptor table could not be read AT ALL — never an answer of "no writers".

    An unread table and an empty one reach a caller as the same empty list, and the
    caller then certifies that nothing holds the database. Raising keeps the two
    apart so a reporting check can stop certifying rather than certify an answer it
    never obtained.
    """


@dataclass(frozen=True, slots=True)
class FdHolder:
    """One process holding a descriptor on the control DB."""

    pid: int
    command: str
    writable: bool

    def __str__(self) -> str:
        return f"{self.command}[{self.pid}] ({'rw' if self.writable else 'ro'})"


class ControlDbWriteDomain:
    """Whether *db_path* is isolated from the host, and who is holding it open.

    *containerized* is injectable because a test process cannot cross the boundary
    whose two sides this class describes.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        env: Mapping[str, str] = os.environ,
        containerized: bool | None = None,
    ) -> None:
        self.db_path = db_path
        self.env = env
        self.containerized = is_running_in_container() if containerized is None else containerized

    @property
    def expected_dir(self) -> Path:
        return control_db_dir(self.env)

    @property
    def in_control_dir(self) -> bool:
        return self.db_path.parent == self.expected_dir

    @property
    def on_host_filesystem(self) -> bool:
        """True when the database lives outside the control-DB directory (the pre-move, bind-mounted layout)."""
        return not self.in_control_dir

    @property
    def wal_eligible(self) -> tuple[bool, str]:
        if not self.containerized:
            return False, "process is outside the container"
        if not self.db_path.is_absolute() or self.db_path.resolve() != self.db_path:
            return False, "database path is relative or symlinked"
        if not self.in_control_dir:
            return False, "database is outside the control-DB directory"
        fstype = self._fstype()
        if fstype is None:
            return False, "mount table is unreadable or does not cover the database"
        if fstype not in _LOCAL_FSTYPES:
            return False, f"filesystem {fstype} is not local"
        return True, "control-DB volume"

    def read_write_holders(self) -> list[FdHolder]:
        """Every PROCESS currently holding *db_path* open for writing.

        Deduplicated per process: one writer with three descriptors on the file is
        one writer, and reporting it three times buries the count that matters.
        """
        return [holder for _, holder in read_write_holders_across([self.db_path])]

    def foreign_writers(self) -> list[FdHolder]:
        """Read-write holders from OUTSIDE the owning domain.

        Inside the container every visible process is the stack itself, so a
        read-write descriptor is the expected, correct state. Outside it, any
        read-write descriptor on the canonical control DB is precisely the fault
        that corrupted this install — there is no legitimate host writer.
        """
        return [] if self.containerized else self.read_write_holders()

    def _fstype(self) -> str | None:
        """The filesystem type carrying the DB's directory, when readable."""
        entries = read_mount_entries()
        if entries is None:
            return None
        entry = mount_entry_for(self.db_path.parent.resolve(), entries)
        return entry.fstype if entry is not None else None


def read_write_holders_across(paths: Sequence[Path]) -> list[tuple[Path, FdHolder]]:
    """Every (path, writing PROCESS) pair across *paths*, reading the descriptor table ONCE.

    The table is the same for every path, so asking per path re-reads all of it per
    path: a real data dir holds 52 control-DB artifacts (the sidecars plus every
    dated rename), which turned one probe into 52 and a doctor run into a 20-second
    one. Matching a whole set against a single read is the same answer at a 52nd of
    the cost.

    Deduplicated per (path, process): one writer with eleven descriptors on a file
    is one writer, and reporting it eleven times buries the count that matters.
    """
    targets = {str(path): path for path in paths}
    if not targets:
        return []
    if not _PROC.is_dir():
        msg = "no /proc on this host — no descriptor view is available"
        raise DescriptorTableUnavailableError(msg)
    writers = {(str(path), holder.pid): (path, holder) for path, holder in _proc_holders(targets) if holder.writable}
    return [writers[key] for key in sorted(writers)]


def _proc_holders(targets: Mapping[str, Path]) -> Iterator[tuple[Path, FdHolder]]:
    for entry in _PROC.iterdir():
        if entry.name.isdigit():
            yield from _holders_for_pid(entry, targets)


def _holders_for_pid(proc_dir: Path, targets: Mapping[str, Path]) -> Iterator[tuple[Path, FdHolder]]:
    try:
        descriptors = list((proc_dir / "fd").iterdir())
    except OSError:
        return
    for descriptor in descriptors:
        try:
            target = targets.get(str(descriptor.readlink()))
            if target is None:
                continue
            flags = _FDINFO_FLAGS.search((proc_dir / "fdinfo" / descriptor.name).read_text(encoding="utf-8"))
        except OSError:
            continue
        writable = flags is not None and int(flags.group(1), 8) & _O_ACCMODE != _O_RDONLY
        yield target, FdHolder(int(proc_dir.name), _command(proc_dir), writable)


def _command(proc_dir: Path) -> str:
    try:
        return (proc_dir / "comm").read_text(encoding="utf-8").strip()
    except OSError:
        return "?"
