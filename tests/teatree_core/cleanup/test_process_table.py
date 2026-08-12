"""The host-aware process table the reclaim guards read (#4244).

The property under test is the failure DIRECTION. A guard that answers "nobody
is inside this checkout" because it is reading a container's own PID namespace
looks identical to one that has checked and found nothing — that is the shape
recorded on #4306, verified on the host and worthless in the venue it ran in. So
every case here asks whether an unanswerable table is reported as unanswerable,
not merely whether the happy path resolves.
"""

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.core.cleanup import process_table
from teatree.core.cleanup.checkout_registry import one_spelling_each
from teatree.core.cleanup.process_table import ProcessTable, read_process_table


@contextmanager
def _child_working_in(directory: Path) -> Iterator[int]:
    """A real child placed in *directory*, so the kernel canonicalises its cwd for us."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"], cwd=directory, stdin=subprocess.DEVNULL
    )
    try:
        yield child.pid
    finally:
        child.kill()
        child.wait()


def _proc_with(root: Path, placements: dict[str, Path]) -> Path:
    """A fake process table: ``{pid: cwd}``, plus a non-numeric entry to skip."""
    for pid, cwd in placements.items():
        (root / pid).mkdir(parents=True)
        (root / pid / "cwd").symlink_to(cwd)
    (root / "self").mkdir(exist_ok=True)
    return root


@pytest.fixture(autouse=True)
def _no_real_proc(tmp_path_factory: pytest.TempPathFactory) -> object:
    """Neither real root leaks in: every case names the table it is testing."""
    absent = tmp_path_factory.mktemp("no-proc") / "absent"
    with (
        patch.object(process_table, "_HOST_PROC_ROOT", absent),
        patch.object(process_table, "_OWN_PROC_ROOT", absent),
        patch.object(process_table, "_CONTAINER_MARKERS", ()),
    ):
        yield


class TestHolds:
    def test_a_process_working_inside_the_directory_holds_it(self, tmp_path: Path) -> None:
        checkout = tmp_path / "checkout"
        table = ProcessTable(frozenset({checkout / "src"}), "/proc")

        assert table.holds(checkout) is True

    def test_a_process_working_beside_the_directory_does_not(self, tmp_path: Path) -> None:
        table = ProcessTable(frozenset({tmp_path / "elsewhere"}), "/proc")

        assert table.holds(tmp_path / "checkout") is False

    def test_a_symlinked_spelling_of_a_held_directory_still_reads_as_held(self, tmp_path: Path) -> None:
        """The reapers ask under whichever spelling sorts first, and the kernel only ever answers canonically."""
        real = tmp_path / "real"
        (real / "src").mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(real)
        with _child_working_in(real / "src") as pid:
            table = ProcessTable(frozenset({Path(f"/proc/{pid}/cwd").readlink()}), "/proc")

        assert table.holds(real.resolve()) is True, "the control: the probe can detect what it looks for"
        assert table.holds(link) is True, "a live agent inside was queued for deletion under this spelling"
        assert one_spelling_each(frozenset({str(real), str(link)})) == [link], (
            "and the failing spelling is the one the reapers are handed"
        )


class TestRefuseReason:
    def test_a_usable_table_refuses_nothing(self) -> None:
        assert ProcessTable(frozenset(), "/proc", ("2 of 9 process(es) did not say",)).refuse_reason() == ""

    def test_an_unusable_table_refuses_with_its_gaps(self) -> None:
        assert ProcessTable(frozenset(), "", ("no readable table",)).refuse_reason() == "no readable table"

    def test_an_unusable_table_with_no_gap_still_refuses(self) -> None:
        assert ProcessTable(frozenset(), "").refuse_reason() == "the process table could not be read"


class TestSourceSelection:
    def test_the_host_mount_wins_over_this_namespace(self, tmp_path: Path) -> None:
        host = _proc_with(tmp_path / "host-proc", {"11": tmp_path / "on-the-host"})
        own = _proc_with(tmp_path / "proc", {"22": tmp_path / "in-the-container"})
        with (
            patch.object(process_table, "_HOST_PROC_ROOT", host),
            patch.object(process_table, "_OWN_PROC_ROOT", own),
        ):
            table = read_process_table()

        assert table.usable
        assert table.holds(tmp_path / "on-the-host")
        assert not table.holds(tmp_path / "in-the-container")

    def test_this_namespace_is_the_answer_when_it_is_itself_the_host(self, tmp_path: Path) -> None:
        own = _proc_with(tmp_path / "proc", {"22": tmp_path / "checkout"})
        with patch.object(process_table, "_OWN_PROC_ROOT", own):
            table = read_process_table()

        assert table.usable
        assert table.holds(tmp_path / "checkout")

    def test_containerised_with_no_host_mount_is_unusable(self, tmp_path: Path) -> None:
        """The #4306 shape: a readable table that answers about the wrong namespace."""
        own = _proc_with(tmp_path / "proc", {"1": tmp_path / "container-entrypoint"})
        marker = tmp_path / ".dockerenv"
        marker.touch()
        with (
            patch.object(process_table, "_OWN_PROC_ROOT", own),
            patch.object(process_table, "_CONTAINER_MARKERS", (marker,)),
        ):
            table = read_process_table()

        assert not table.usable, "a container's own namespace must never pass as the host's"
        assert any("host process table" in gap for gap in table.gaps)

    def test_no_readable_table_anywhere_is_unusable(self) -> None:
        table = read_process_table()

        assert not table.usable
        assert table.gaps


class TestPartialVisibility:
    def test_a_table_no_process_answers_is_unusable(self, tmp_path: Path) -> None:
        """Listed-but-mute is the blind case, and it must not read as "nobody is inside"."""
        host = tmp_path / "host-proc"
        (host / "42").mkdir(parents=True)  # a pid dir with no cwd/exe link at all
        with patch.object(process_table, "_HOST_PROC_ROOT", host):
            table = read_process_table()

        assert not table.usable
        assert any("would say where it is running" in gap for gap in table.gaps)

    def test_some_processes_declining_is_a_gap_not_a_refusal(self, tmp_path: Path) -> None:
        """Only the pid's own uid may read its links, so a shared box always has mute pids."""
        host = _proc_with(tmp_path / "host-proc", {"11": tmp_path / "checkout"})
        (host / "12").mkdir()
        with patch.object(process_table, "_HOST_PROC_ROOT", host):
            table = read_process_table()

        assert table.usable
        assert table.holds(tmp_path / "checkout")
        assert any("did not say where they run" in gap for gap in table.gaps)

    def test_the_binary_a_process_runs_places_it_too(self, tmp_path: Path) -> None:
        """A daemon started from a venv keeps its cwd elsewhere; its ``exe`` is the tell."""
        host = tmp_path / "host-proc"
        (host / "7").mkdir(parents=True)
        (host / "7" / "cwd").symlink_to(tmp_path / "elsewhere")
        (host / "7" / "exe").symlink_to(tmp_path / "checkout" / ".venv" / "bin" / "python")
        with patch.object(process_table, "_HOST_PROC_ROOT", host):
            table = read_process_table()

        assert table.holds(tmp_path / "checkout")
