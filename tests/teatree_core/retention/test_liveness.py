"""The open-file probe's own contract: RESOLUTION is the witness, and it fails closed (#4165).

The sweep-level consequences — removable, ``probe_gap``, what ``apply()`` reclaims —
are in ``test_scratch.py``; this file pins the probe's three-valued answer directly.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase

from teatree.core.retention.liveness import held_paths

_HEADER = "Num       RefCount Protocol Flags    Type St Inode Path"


class HeldPathsTests(SimpleTestCase):
    def setUp(self) -> None:
        self.proc = Path(self.enterContext(TemporaryDirectory()))
        self.elsewhere = Path(self.enterContext(TemporaryDirectory()))

    def _pid(self, pid: str, source: str = "fd") -> Path:
        directory = self.proc / pid / source
        directory.mkdir(parents=True)
        return directory

    def test_a_resolvable_fd_reports_the_path_it_names(self) -> None:
        target = self.elsewhere / "open.db"
        (self._pid("101") / "3").symlink_to(target)

        assert held_paths(self.proc) == frozenset({str(target)})

    def test_a_listed_but_unresolvable_entry_answers_none(self) -> None:
        # A regular file where a symlink belongs: iterdir() lists it, readlink()
        # raises EINVAL — the shape 35 of 325 host pids present at uid 1001.
        (self._pid("101") / "3").write_bytes(b"")

        assert held_paths(self.proc) is None

    def test_one_blind_pid_blinds_the_probe_despite_an_answering_sibling(self) -> None:
        (self._pid("101") / "3").symlink_to(self.elsewhere / "open.db")
        (self._pid("202") / "3").write_bytes(b"")

        assert held_paths(self.proc) is None

    def test_an_empty_readable_source_is_an_answer_contributing_nothing(self) -> None:
        self._pid("101")  # a kernel thread's shape: readable, and genuinely empty

        assert held_paths(self.proc) == frozenset()

    def test_a_pid_with_no_readable_source_at_all_is_skipped_not_blinding(self) -> None:
        (self._pid("101") / "3").symlink_to(self.elsewhere / "open.db")
        (self.proc / "202").mkdir()  # another uid's pid: nothing to read

        assert held_paths(self.proc) == frozenset({str(self.elsewhere / "open.db")})

    def test_a_cwd_alone_answers_for_its_pid(self) -> None:
        (self.proc / "101").mkdir()
        (self.proc / "101" / "cwd").symlink_to(self.elsewhere)

        assert held_paths(self.proc) == frozenset({str(self.elsewhere)})

    def test_a_table_with_no_numeric_pid_is_not_a_procfs(self) -> None:
        (self.proc / "sys").mkdir()

        assert held_paths(self.proc) is None

    def test_an_unlistable_root_blinds_outright(self) -> None:
        assert held_paths(self.proc / "absent") is None

    def test_a_map_files_entry_counts_as_a_holder(self) -> None:
        mapped = self.elsewhere / "mapped.so"
        (self._pid("101", "map_files") / "7f0000-7f1000").symlink_to(mapped)

        assert held_paths(self.proc) == frozenset({str(mapped)})

    def test_a_bound_socket_supplies_a_path_but_never_vouches_for_the_probe(self) -> None:
        """``<pid>/net/unix`` is 0444 where the gated sources are 0500 behind ptrace.

        It answers for every pid whatever this uid can reach, so counting it as a
        witness would retire the fail-closed guard on any real ``/proc``.
        """
        pid = self.proc / "101"
        (pid / "net").mkdir(parents=True)
        (pid / "net" / "unix").write_text(
            "Num       RefCount Protocol Flags    Type St Inode Path\n"
            f"0000000000000000: 00000002 00000000 00000000 0001 01 12345 {self.elsewhere / 'app.sock'}\n",
            encoding="utf-8",
        )

        assert held_paths(self.proc) is None


class SocketTableParserTests(SimpleTestCase):
    """``<pid>/net/unix`` is kernel-formatted text, and the parser's tolerances were unpinned.

    Each is conservative in direction — a malformed row yields no bind path rather
    than a wrong one — but the probe's whole credibility is that it never mistakes a
    held path for a free one, so its parser's edges are pinned rather than assumed.
    """

    def setUp(self) -> None:
        self.proc = Path(self.enterContext(TemporaryDirectory()))
        self.elsewhere = Path(self.enterContext(TemporaryDirectory()))
        self.answered = self.elsewhere / "answered"
        pid = self.proc / "101"
        (pid / "fd").mkdir(parents=True)
        (pid / "fd" / "0").symlink_to(self.answered)
        self.table = pid / "net" / "unix"
        self.table.parent.mkdir(parents=True)

    def _row(self, path: str, *, fields: int = 8) -> str:
        columns = ("0000000000000000:", "00000002", "00000000", "00000000", "0001", "01", "12345", path)
        return " ".join(columns[:fields])

    def test_a_truncated_row_is_skipped_rather_than_crashing_the_probe(self) -> None:
        held = self.elsewhere / "held.sock"
        self.table.write_text("\n".join((_HEADER, self._row("", fields=5), self._row(str(held)))), encoding="utf-8")

        assert held_paths(self.proc) == frozenset({str(self.answered), str(held)})

    def test_the_header_row_is_never_read_as_a_bind_path(self) -> None:
        # A header whose own last column is a path-shaped token parses as a valid
        # 8-field row, so only dropping line 0 keeps it out of the held set.
        self.table.write_text("Num RefCount Protocol Flags Type St Inode /header.sock\n", encoding="utf-8")

        assert held_paths(self.proc) == frozenset({str(self.answered)})

    def test_a_non_utf8_byte_in_a_bind_path_does_not_raise(self) -> None:
        undecodable = b"/run/\xff.sock"
        self.table.write_bytes(
            _HEADER.encode() + b"\n0000000000000000: 00000002 00000000 00000000 0001 01 12345 " + undecodable + b"\n"
        )

        assert held_paths(self.proc) == frozenset({str(self.answered), undecodable.decode(errors="replace")})
