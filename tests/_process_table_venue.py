"""Pin the process table's VENUE, so a test's answer does not depend on where it runs.

:func:`teatree.core.cleanup.process_table.read_process_table` picks its source
from the box: the host bind mount, else this namespace unless a container marker
says that namespace is the wrong one. A test that blinds only one of those passes
in a container and fails on a host — the exact venue-dependence #4244 is about.
Both helpers name every source, so the answer is the test's own.
"""

import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from teatree.core.cleanup import process_table
from teatree.core.cleanup.process_table import ProcessTable

#: What the synthetic tables call THIS process. Deliberately not ``os.getpid()``:
#: a bind-mounted host ``/proc`` numbers this process by its HOST pid, so a reader
#: that trusts ``os.getpid()`` names a different process entirely and must go red.
#: Above Linux's pid ceiling (2**22), so it never equals a worker's real pid or a pid a test places.
THIS_PROCESS_PID = str(2**22 + 1)


@contextmanager
def pinned_process_table(*, venue: Path, host: Path) -> Generator[None]:
    """Both roots named, so which table answers is the test's choice and not the box's."""
    with (
        patch.object(process_table, "_HOST_PROC_ROOT", host),
        patch.object(process_table, "_OWN_PROC_ROOT", venue),
        patch.object(process_table, "_CONTAINER_MARKERS", ()),
    ):
        yield


def this_process_in(root: Path) -> Path:
    """Place THIS process in *root* the way a procfs does — a ``self`` link and a pid dir.

    Every synthetic table needs it, because a reaper that cannot tell which pid is
    its own folds its own open descriptors into the answer and reads its own
    deletion lock as somebody else's work.
    """
    own = root / THIS_PROCESS_PID
    (own / "fd").mkdir(parents=True, exist_ok=True)
    self_link = root / "self"
    if not self_link.exists():
        self_link.symlink_to(THIS_PROCESS_PID)
    return own


def holding(pid_dir: Path, path: Path, *, descriptor: str = "3") -> None:
    """Give *pid_dir* an open descriptor on *path* — the shape a directory lock presents."""
    fds = pid_dir / "fd"
    fds.mkdir(parents=True, exist_ok=True)
    (fds / descriptor).symlink_to(path)


@contextmanager
def blinded_process_table(absent: Path) -> Generator[None]:
    """No table this venue can read — the fail-closed case."""
    with pinned_process_table(venue=absent, host=absent):
        yield


def usable_process_table(root: Path, *, working_in: Path) -> Path:
    """A readable host table: one OTHER process placed at *working_in*, plus this one."""
    other = root / "1"
    other.mkdir(parents=True)
    (other / "cwd").symlink_to(working_in)
    (other / "exe").symlink_to(working_in / "bin" / "process")
    this_process_in(root)
    return root


@contextmanager
def nested_venue(root: Path, *, host_nspid: str = "NSpid:\t41\t7\n") -> Generator[None]:
    """This process seen from the host table at *host_nspid*, in venue pid namespace ``pid:[42]``."""
    host = usable_process_table(root / "host", working_in=root / "elsewhere")
    (host / THIS_PROCESS_PID / "status").write_text(host_nspid, encoding="utf-8")
    venue = this_process_in(root / "venue").parent
    (venue / "self" / "ns").mkdir(parents=True)
    (venue / "self" / "ns" / "pid").symlink_to("pid:[42]")
    with pinned_process_table(venue=venue, host=host), patch.dict(os.environ, {"TEATREE_HOST_OS": "Linux"}):
        yield


def table_with_unreadable_pid(
    tmp_path: Path,
    *,
    nspid: str = "",
    name: str = "claude",
    status: str | None = None,
    uid_offset: int = 0,
) -> ProcessTable:
    host = usable_process_table(tmp_path / "host-proc", working_in=tmp_path / "elsewhere")
    blind = host / "21"
    blind.mkdir()
    (blind / "comm").write_text(f"{name}\n", encoding="utf-8")
    (blind / "status").write_text(
        status if status is not None else f"State:\tS\nThreads:\t1\n{nspid}", encoding="utf-8"
    )
    hidden = blind / "fd" / "0"
    hidden.parent.mkdir()
    hidden.symlink_to(tmp_path / "unknown")
    readlink = Path.readlink

    def gated_readlink(path: Path) -> Path:
        if path == hidden:
            raise PermissionError
        return readlink(path)

    with (
        pinned_process_table(venue=tmp_path / "absent", host=host),
        patch.object(Path, "readlink", gated_readlink),
        patch.object(process_table.os, "geteuid", return_value=blind.stat().st_uid + uid_offset),
        patch.dict(os.environ, {"TEATREE_HOST_OS": "Linux"}),
    ):
        return process_table.read_process_table(exclude_ancestors=True)
