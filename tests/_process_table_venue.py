"""Pin the process table's VENUE, so a test's answer does not depend on where it runs.

:func:`teatree.core.cleanup.process_table.read_process_table` picks its source
from the box: the host bind mount, else this namespace unless a container marker
says that namespace is the wrong one. A test that blinds only one of those passes
in a container and fails on a host — the exact venue-dependence #4244 is about.
Both helpers name every source, so the answer is the test's own.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from teatree.core.cleanup import process_table


@contextmanager
def blinded_process_table(absent: Path) -> Iterator[None]:
    """No table this venue can read — the fail-closed case."""
    with (
        patch.object(process_table, "_HOST_PROC_ROOT", absent),
        patch.object(process_table, "_OWN_PROC_ROOT", absent),
        patch.object(process_table, "_CONTAINER_MARKERS", ()),
    ):
        yield


def usable_process_table(root: Path, *, working_in: Path) -> Path:
    """A readable host table holding one process placed at *working_in*."""
    (root / "1").mkdir(parents=True)
    (root / "1" / "cwd").symlink_to(working_in)
    return root
