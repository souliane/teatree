"""Pin the process table's VENUE, so a test's answer does not depend on where it runs.

:func:`teatree.core.cleanup.process_table.read_process_table` picks its source
from the box: the host bind mount, else this namespace unless a container marker
says that namespace is the wrong one. A test that blinds only one of those passes
in a container and fails on a host — the exact venue-dependence #4244 is about.
Both helpers name every source, so the answer is the test's own.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from teatree.core.cleanup import process_table

#: What the synthetic tables call THIS process. Deliberately not ``os.getpid()``:
#: a bind-mounted host ``/proc`` numbers this process by its HOST pid, so a reader
#: that trusts ``os.getpid()`` names a different process entirely and must go red.
THIS_PROCESS_PID = str(os.getpid() + 1)


@contextmanager
def pinned_process_table(*, venue: Path, host: Path) -> Iterator[None]:
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
def blinded_process_table(absent: Path) -> Iterator[None]:
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
