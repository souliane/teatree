"""The open-file probe's own contract: RESOLUTION is the witness, and it fails closed (#4165).

The sweep-level consequences — removable, ``probe_gap``, what ``apply()`` reclaims —
are in ``test_scratch.py``; this file pins the probe's three-valued answer directly.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase

from teatree.core.retention.liveness import ProcessTableView, held_paths, normalized_spelling
from tests._procfs import answering_pid

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

        view = held_paths(self.proc)

        assert view.held == frozenset({str(target)})
        assert view.sighted is True

    def test_a_listed_but_unresolvable_entry_answers_none(self) -> None:
        # A regular file where a symlink belongs: iterdir() lists it, readlink()
        # raises EINVAL — the shape 35 of 325 host pids present at uid 1001.
        (self._pid("101") / "3").write_bytes(b"")

        assert held_paths(self.proc).sighted is False

    def test_one_blind_pid_blinds_the_probe_despite_an_answering_sibling(self) -> None:
        (self._pid("101") / "3").symlink_to(self.elsewhere / "open.db")
        (self._pid("202") / "3").write_bytes(b"")

        assert held_paths(self.proc).sighted is False

    def test_an_empty_readable_source_is_an_answer_contributing_nothing(self) -> None:
        self._pid("101")  # a kernel thread's shape: readable, and genuinely empty

        assert held_paths(self.proc) == ProcessTableView(held=frozenset(), answered_pids=1, unknowable_pids=0)

    def test_a_pid_with_no_readable_source_at_all_is_skipped_not_blinding(self) -> None:
        (self._pid("101") / "3").symlink_to(self.elsewhere / "open.db")
        (self.proc / "202").mkdir()  # another uid's pid: nothing to read

        assert held_paths(self.proc).held == frozenset({str(self.elsewhere / "open.db")})

    def test_a_cwd_alone_answers_for_its_pid(self) -> None:
        (self.proc / "101").mkdir()
        (self.proc / "101" / "cwd").symlink_to(self.elsewhere)

        assert held_paths(self.proc).held == frozenset({str(self.elsewhere)})

    def test_a_table_with_no_numeric_pid_is_not_a_procfs(self) -> None:
        (self.proc / "sys").mkdir()

        assert held_paths(self.proc).sighted is False

    def test_an_unlistable_root_blinds_outright(self) -> None:
        assert held_paths(self.proc / "absent").sighted is False

    def test_a_map_files_entry_counts_as_a_holder(self) -> None:
        mapped = self.elsewhere / "mapped.so"
        (self._pid("101", "map_files") / "7f0000-7f1000").symlink_to(mapped)

        assert held_paths(self.proc).held == frozenset({str(mapped)})

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

        assert held_paths(self.proc).sighted is False


class FdPermissionLadderTests(SimpleTestCase):
    """The three-way control: a holder this uid can read, cannot resolve, and cannot LIST.

    All three are the same question — can this probe see what pid 202 holds? — and
    only the first has an answer. The 0500 arm is the positive control proving the
    harness can see a holder at all; without it a green on the other two arms would
    be indistinguishable from a fixture that never held anything.
    """

    def setUp(self) -> None:
        self.proc = Path(self.enterContext(TemporaryDirectory()))
        self.scratch = Path(self.enterContext(TemporaryDirectory()))
        self.victim = self.scratch / "held.db"
        self.victim.write_bytes(b"")
        answering_pid(self.proc, self.scratch / "unrelated")
        self.holder_fd = self.proc / "202" / "fd"
        self.holder_fd.mkdir(parents=True)
        (self.holder_fd / "3").symlink_to(self.victim)

    def _chmod(self, mode: int) -> None:
        self.holder_fd.chmod(mode)
        self.addCleanup(self.holder_fd.chmod, 0o700)

    def test_a_readable_holder_reports_the_path_it_holds(self) -> None:
        self._chmod(0o500)

        view = held_paths(self.proc)

        assert view.sighted is True
        assert str(self.victim) in view.held

    def test_a_listable_but_unresolvable_holder_is_unknowable(self) -> None:
        self._chmod(0o400)

        view = held_paths(self.proc)

        assert view.sighted is False
        assert view.unknowable_pids == 1
        assert "202" in view.unknowable_reason

    def test_an_unlistable_holder_is_unknowable_rather_than_silently_dropped(self) -> None:
        # The CRITICAL: this arm used to yield a SIGHTED view whose held set simply
        # omitted the victim, so the sweep deleted a file a live process held open.
        self._chmod(0o000)

        view = held_paths(self.proc)

        assert view.sighted is False
        assert str(self.victim) not in view.held
        assert view.unknowable_pids == 1

    def test_a_pid_that_exits_mid_walk_is_an_absence_not_a_blind_spot(self) -> None:
        (self.proc / "303").mkdir()  # ENOENT on every source: the pid is already gone

        view = held_paths(self.proc)

        assert view.sighted is True
        assert view.unknowable_pids == 0


class NormalizedSpellingTests(SimpleTestCase):
    """One spelling for both sides of a held-path comparison — parent resolved, leaf not."""

    def setUp(self) -> None:
        self.real = Path(self.enterContext(TemporaryDirectory()))
        self.link = self.real.parent / f"{self.real.name}-link"
        self.link.symlink_to(self.real)
        self.addCleanup(self.link.unlink)

    def test_a_symlinked_parent_resolves_to_the_same_spelling_as_the_real_one(self) -> None:
        assert normalized_spelling(str(self.link / "entry")) == str(self.real / "entry")

    def test_a_leaf_symlink_stays_itself_because_the_sweep_unlinks_it_unfollowed(self) -> None:
        leaf = self.real / "leaf"
        leaf.symlink_to(self.real / "target")

        assert normalized_spelling(str(leaf)) == str(leaf)

    def test_a_kernel_pseudo_target_is_not_a_path_and_is_left_verbatim(self) -> None:
        assert normalized_spelling("socket:[12345]") == "socket:[12345]"


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

        assert held_paths(self.proc).held == frozenset({str(self.answered), str(held)})

    def test_the_header_row_is_never_read_as_a_bind_path(self) -> None:
        # A header whose own last column is a path-shaped token parses as a valid
        # 8-field row, so only dropping line 0 keeps it out of the held set.
        self.table.write_text("Num RefCount Protocol Flags Type St Inode /header.sock\n", encoding="utf-8")

        assert held_paths(self.proc).held == frozenset({str(self.answered)})

    def test_a_non_utf8_byte_in_a_bind_path_does_not_raise(self) -> None:
        undecodable = b"/run/\xff.sock"
        self.table.write_bytes(
            _HEADER.encode() + b"\n0000000000000000: 00000002 00000000 00000000 0001 01 12345 " + undecodable + b"\n"
        )

        assert held_paths(self.proc).held == frozenset({str(self.answered), undecodable.decode(errors="replace")})
