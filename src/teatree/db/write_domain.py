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
* which processes hold a READ-WRITE descriptor on it, read from ``/proc`` where it
    exists and from ``lsof`` where it does not, so the answer is available on both
    sides of the boundary.
"""

import os
import re
import shutil
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from teatree.docker.workflow import is_running_in_container
from teatree.paths import control_db_dir
from teatree.utils.run import CommandFailedError, run_allowed_to_fail

_PROC = Path("/proc")
_O_ACCMODE = 0o3
_O_RDONLY = 0o0
_LSOF_TIMEOUT_SECONDS = 10

#: `lsof`'s access column: `u` is read+write, `w` write-only, `r` read-only.
_LSOF_WRITABLE_MODES = frozenset({"u", "w"})

#: Filesystems Docker Desktop serves a host directory over. A control DB sitting on
#: one of these is still on the host's disk, whatever its path says.
_HOST_SHARED_FSTYPES = frozenset({"virtiofs", "fuse.grpcfuse", "osxfs", "9p", "fakeowner"})

_FDINFO_FLAGS = re.compile(r"^flags:\s*(\d+)$", re.MULTILINE)


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
    def on_host_filesystem(self) -> bool:
        """True when the database is somewhere a host process can name or reach.

        Two independent ways it can be: living outside the control-DB directory
        entirely (the pre-move layout, a bind-mounted data dir), or living at the
        right path on a filesystem the shared-folder layer serves from the host.
        """
        return self.db_path.parent != self.expected_dir or self._fstype() in _HOST_SHARED_FSTYPES

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

    def _fstype(self) -> str:
        """The filesystem type carrying the DB's directory; ``""`` when unknowable.

        ``/proc/self/mountinfo`` is Linux-only, which is the right scope: the check
        exists to catch a Docker-Desktop shared folder mounted INSIDE a container.
        """
        mountinfo = _PROC / "self" / "mountinfo"
        try:
            lines = mountinfo.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        best_mountpoint, best_fstype = "", ""
        target = str(self.db_path.parent)
        for line in lines:
            head, _, tail = line.partition(" - ")
            fields, tail_fields = head.split(), tail.split()
            if len(fields) < 5 or not tail_fields:  # noqa: PLR2004 — mountinfo's mount-point column index
                continue
            mountpoint = fields[4]
            if (target == mountpoint or target.startswith(f"{mountpoint.rstrip('/')}/")) and len(mountpoint) >= len(
                best_mountpoint
            ):
                best_mountpoint, best_fstype = mountpoint, tail_fields[0]
        return best_fstype


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
    found = _proc_holders(targets) if _PROC.is_dir() else _lsof_holders(targets)
    writers = {(str(path), holder.pid): (path, holder) for path, holder in found if holder.writable}
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


def _lsof_holders(targets: Mapping[str, Path]) -> Iterator[tuple[Path, FdHolder]]:
    """Parse one ``lsof -F pcan`` over every target — the only descriptor view a non-Linux host offers.

    ``lsof`` exits 1 when nothing holds the files, which is an ANSWER (no holders),
    not a failure — hence the widened *expected_codes*. The ``n`` (name) field
    arrives AFTER the ``a`` (access) field of the same record, so a record is only
    complete once its name lands; that is what lets one invocation cover many files.
    """
    lsof = shutil.which("lsof")
    if lsof is None:
        return
    try:
        result = run_allowed_to_fail(
            [lsof, "-F", "pcan", "--", *sorted(targets)],
            expected_codes=None,
            timeout=_LSOF_TIMEOUT_SECONDS,
        )
    except (OSError, CommandFailedError):
        return
    pid, command, writable = 0, "?", False
    for line in result.stdout.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p" and value.isdigit():
            pid, command = int(value), "?"
        elif tag == "c":
            command = value
        elif tag == "a":
            writable = value.strip() in _LSOF_WRITABLE_MODES
        elif tag == "n" and pid:
            target = targets.get(value)
            if target is not None:
                yield target, FdHolder(pid, command, writable)
