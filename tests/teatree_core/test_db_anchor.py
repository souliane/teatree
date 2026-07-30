"""Regression tests for the cross-DB guard on the lifecycle/ship path (#779).

Each worktree has an isolated control DB. ``lifecycle visit-phase`` and
``pr create`` historically resolved the DB from the *process*: the true
canonical DB when proxied through ``t3 <ov>`` (always via the main clone),
but the worktree's own isolated DB when run via ``uv run manage.py`` from the
worktree. The mismatch is SYMMETRIC — a maker's ``testing``/``retro`` and a
reviewer's ``reviewing`` recorded from a worktree all land in an isolated DB
the shipping gate (canonical DB) never consults, producing a verbatim
``missing: [...]`` block whose phases were recorded, just in the wrong DB.
This cost multiple multi-cycle misdiagnoses (#764, #628, #769, #777, #778).

These tests pin the contract: a ticket-bound lifecycle/ship operation running
against a worktree-isolated DB must REFUSE with an actionable error naming the
canonical DB and the correct command — never silently read/write the wrong
DB — in BOTH directions (maker testing/retro AND reviewer reviewing). The
canonical-DB path (the global proxy) must pass through untouched.
"""

from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree import paths
from teatree.core.models import Session, Ticket, Worktree
from teatree.core.provision import db_anchor
from teatree.core.provision.db_anchor import (
    WrongWorktreeDBError,
    _is_worktree_isolated_db,
    assert_lifecycle_db_is_canonical,
)
from teatree.paths import expected_db_for_repo


def _make_worktree_repo(root: Path) -> Path:
    """A git *worktree* has a ``.git`` file (not a dir) — drives auto-isolation."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8")
    return root


def _make_primary_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir()
    return root


class TestExpectedDbForRepo:
    """The deterministic anchor: same resolution the process uses, parameterised."""

    def test_worktree_repo_resolves_to_isolated_db(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        wt = _make_worktree_repo(tmp_path / "wt")
        db = expected_db_for_repo(wt, env={}, home=home)
        # Auto-isolated worktree DB lives under the sibling isolation root,
        # never under the canonical data dir.
        assert db.name == "db.sqlite3"
        with pytest.raises(ValueError, match=r"subpath|does not start with"):
            db.resolve().relative_to((home / ".local" / "share" / "teatree").resolve())

    def test_primary_repo_resolves_to_canonical_db(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        main = _make_primary_repo(tmp_path / "main")
        db = expected_db_for_repo(main, env={}, home=home)
        assert db == home / ".local" / "share" / "teatree" / "db.sqlite3"

    def test_resolution_is_deterministic_per_repo_path(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        wt = _make_worktree_repo(tmp_path / "wt")
        assert expected_db_for_repo(wt, env={}, home=home) == expected_db_for_repo(wt, env={}, home=home)


class TestIsWorktreeIsolatedDb:
    """The production trip classifier: live connection DB vs isolation root."""

    def test_real_isolated_db_under_root_trips(self, tmp_path: Path) -> None:
        root = tmp_path / "teatree-worktrees"
        db = root / "abc123" / "db.sqlite3"
        assert _is_worktree_isolated_db(str(db), isolation_root=root) is True

    def test_true_canonical_db_outside_root_does_not_trip(self, tmp_path: Path) -> None:
        root = tmp_path / "teatree-worktrees"
        canonical = tmp_path / "teatree" / "db.sqlite3"
        assert _is_worktree_isolated_db(str(canonical), isolation_root=root) is False

    def test_in_memory_test_db_never_trips(self, tmp_path: Path) -> None:
        """pytest-django's sqlite test DB is ``:memory:`` — must stay inert."""
        root = tmp_path / "teatree-worktrees"
        assert _is_worktree_isolated_db(":memory:", isolation_root=root) is False
        assert _is_worktree_isolated_db("file::memory:?cache=shared", isolation_root=root) is False

    def test_empty_name_does_not_trip(self, tmp_path: Path) -> None:
        assert _is_worktree_isolated_db("", isolation_root=tmp_path) is False


class TestAssertLifecycleDbIsCanonical(TestCase):
    """The guard wired into ``visit-phase`` and ``pr create`` (#779).

    The ``auto_isolated`` kwarg forces the decision deterministically without
    rebinding a real sqlite connection; production omits it and classifies the
    live Django connection's DB path against the worktree isolation root.
    """

    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    def _ticket_with_worktree(self, worktree_path: str) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="org/repo",
            branch="feature-branch",
            extra={"worktree_path": worktree_path},
        )
        return ticket

    def test_refuses_maker_phase_visit_from_worktree_isolated_db(self) -> None:
        """Direction 1 (#769/#777): maker testing/retro from a worktree — isolated DB, invisible to the gate."""
        wt = str(_make_worktree_repo(self.tmp_path / "wt"))
        ticket = self._ticket_with_worktree(wt)
        isolated_db = self.tmp_path / "teatree-worktrees" / "abc123" / "db.sqlite3"
        canonical_db = self.tmp_path / "home" / ".local" / "share" / "teatree" / "db.sqlite3"

        with pytest.raises(WrongWorktreeDBError) as excinfo:
            assert_lifecycle_db_is_canonical(
                ticket,
                auto_isolated=True,
                active_db=isolated_db,
                canonical_db=canonical_db,
            )

        msg = str(excinfo.value)
        # The error must name BOTH the isolated DB in use and the canonical DB
        # the gate reads, plus the ticket's worktree and the correct command,
        # so the operator acts without another multi-cycle misdiagnosis.
        assert str(isolated_db) in msg
        assert str(canonical_db) in msg
        assert wt in msg
        assert "t3 <overlay> lifecycle visit-phase" in msg
        assert "t3 <overlay> pr create" in msg

    def test_refuses_reviewer_phase_visit_from_worktree_isolated_db(self) -> None:
        """Direction 2 (#764): reviewer reviewing from a worktree — same isolated-DB invisibility, opposite actor."""
        wt = str(_make_worktree_repo(self.tmp_path / "wt"))
        ticket = self._ticket_with_worktree(wt)

        with pytest.raises(WrongWorktreeDBError):
            assert_lifecycle_db_is_canonical(
                ticket,
                auto_isolated=True,
                active_db=self.tmp_path / "iso" / "db.sqlite3",
                canonical_db=self.tmp_path / "canon" / "db.sqlite3",
            )

    def test_passes_on_canonical_db_the_global_proxy_path(self) -> None:
        """``t3 <ov>`` proxies through the main clone → canonical DB → never blocked."""
        ticket = self._ticket_with_worktree(str(_make_worktree_repo(self.tmp_path / "wt")))
        # Must NOT raise: this is the DB the gate reads.
        assert_lifecycle_db_is_canonical(ticket, auto_isolated=False)

    def test_passes_when_ticket_has_no_worktree_on_canonical_db(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        assert_lifecycle_db_is_canonical(ticket, auto_isolated=False)

    def test_production_default_is_inert_under_the_test_runner(self) -> None:
        """No injected args → classifies the LIVE connection.

        Under pytest-django that is the ``:memory:`` test DB, so the guard
        must not fire — otherwise every existing ``pr create`` /
        ``visit-phase`` test run from a worktree checkout would falsely trip
        it (the regression observed while wiring this guard in).
        """
        ticket = self._ticket_with_worktree(str(_make_worktree_repo(self.tmp_path / "wt")))
        # Must NOT raise: the live test connection is in-memory, not a real
        # per-worktree isolated db.sqlite3 under the isolation root.
        assert_lifecycle_db_is_canonical(ticket)

    def test_error_names_unknown_when_worktree_path_unrecorded(self) -> None:
        """A Worktree row without a recorded path still refuses, degrading the name to ``<unknown>``."""
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="org/repo",
            branch="feature-branch",
            extra={},
        )
        with pytest.raises(WrongWorktreeDBError, match="<unknown>"):
            assert_lifecycle_db_is_canonical(
                ticket,
                auto_isolated=True,
                active_db=self.tmp_path / "iso" / "db.sqlite3",
                canonical_db=self.tmp_path / "canon" / "db.sqlite3",
            )


class TestGuardWiredThroughCommandEntrypoints(TestCase):
    """HIGH-1: drive the guard through the REAL wired call sites (#779).

    The unit tests above inject ``auto_isolated=True`` directly, so deleting
    the ``assert_lifecycle_db_is_canonical(ticket)`` line in ``lifecycle.py``
    / ``pr.py`` would leave them green while re-introducing the silent
    cross-DB bug. These exercise the production default path through
    ``call_command`` with the live connection's DB classified as a real
    per-worktree isolated DB (the ``uv run`` -from-a-worktree condition), so
    un-wiring a call site makes them RED.
    """

    @pytest.fixture(autouse=True)
    def _force_isolated_active_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Make the production default (`_active_db_path`) report a real
        # db.sqlite3 *under the isolation root* — exactly what the live
        # connection looks like when `uv run manage.py` runs from a worktree.
        isolated_db = paths.auto_isolated_worktrees_dir() / "deadbeef1234" / "db.sqlite3"
        monkeypatch.setattr(db_anchor, "_active_db_path", lambda: str(isolated_db))

    def _ticket_with_worktree(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="org/repo",
            branch="feature-branch",
            extra={"worktree_path": "/some/worktree"},
        )
        return ticket

    def test_lifecycle_visit_phase_refuses_on_isolated_db(self) -> None:
        ticket = self._ticket_with_worktree()
        with pytest.raises(WrongWorktreeDBError):
            call_command("lifecycle", "visit-phase", str(ticket.pk), "testing")

    def test_pr_create_refuses_on_isolated_db(self) -> None:
        ticket = self._ticket_with_worktree()
        Session.objects.create(ticket=ticket, overlay="test")
        with pytest.raises(WrongWorktreeDBError):
            call_command("pr", "create", str(ticket.pk))
