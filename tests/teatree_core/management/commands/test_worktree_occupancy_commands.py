"""The operator surface over a checkout's occupancy claim (souliane/teatree#3952).

The dispatched lane holds its claim for the length of a run; a hand-driven lane —
an operator, or an agent working a branch at the raw-git level — has no run to
scope it to, so it reaches the same claim through these commands. Every assertion
here is on the CLI's ANSWER (who holds it, what was refused, what was freed),
because that answer is the whole point: a second actor must be told the holder
rather than pointed into the tree.
"""

import shutil
import tempfile
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.management.commands._worktree_occupancy import _worktree_for_path
from teatree.core.models import Worktree
from teatree.core.worktree.checkout_state import checkout_state
from teatree.core.worktree.occupancy import acquire, occupancy_holder
from tests._mount_table import patched_mount_table
from tests.factories import TicketFactory, WorktreeFactory
from tests.teatree_core.models._shared import _init_repo_with_branch


class _CommandCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ticket = TicketFactory()
        self.checkout = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: self.checkout.exists() and self.checkout.rmdir())
        self.worktree = WorktreeFactory(ticket=self.ticket, extra={"worktree_path": str(self.checkout)})

    def run_cmd(self, *args: str) -> str:
        from io import StringIO  # noqa: PLC0415 — test-local

        out, err = StringIO(), StringIO()
        call_command("worktree", *args, stdout=out, stderr=err)
        return out.getvalue() + err.getvalue()

    def fresh(self) -> Worktree:
        return Worktree.objects.get(pk=self.worktree.pk)


class OccupancyListTests(_CommandCase):
    def test_an_unheld_workspace_reports_nothing_held(self) -> None:
        assert "No checkout is currently held" in self.run_cmd("occupancy")

    def test_a_held_checkout_is_listed_with_its_holder(self) -> None:
        acquire(self.worktree, holder="task:7", holder_session="session-A")
        out = self.run_cmd("occupancy")
        assert str(self.checkout) in out
        assert "task:7" in out
        assert "session-A" in out


class ClaimTests(_CommandCase):
    def test_claiming_a_free_checkout_records_the_holder(self) -> None:
        self.run_cmd("claim-occupancy", str(self.checkout), "--holder", "operator:me")
        held = occupancy_holder(self.fresh())
        assert held is not None
        assert held.holder == "operator:me"

    def test_claiming_an_occupied_checkout_is_refused_naming_the_holder(self) -> None:
        acquire(self.worktree, holder="task:7", holder_session="session-A")
        with pytest.raises(SystemExit) as caught:
            self.run_cmd("claim-occupancy", str(self.checkout), "--holder", "operator:me")
        assert caught.value.code == 1
        held = occupancy_holder(self.fresh())
        assert held is not None
        assert held.holder == "task:7"

    def test_an_anonymous_claim_is_refused(self) -> None:
        with pytest.raises(SystemExit):
            self.run_cmd("claim-occupancy", str(self.checkout))
        assert occupancy_holder(self.fresh()) is None

    def test_an_unregistered_path_is_refused(self) -> None:
        with pytest.raises(SystemExit):
            self.run_cmd("claim-occupancy", "/nowhere/at/all", "--holder", "operator:me")


class ReleaseTests(_CommandCase):
    def test_release_frees_the_incumbents_claim_and_names_it(self) -> None:
        acquire(self.worktree, holder="task:7", holder_session="session-A")
        out = self.run_cmd("release-occupancy", str(self.checkout))
        assert "task:7" in out
        assert occupancy_holder(self.fresh()) is None

    def test_releasing_an_unheld_checkout_says_so(self) -> None:
        assert "not held" in self.run_cmd("release-occupancy", str(self.checkout))

    def test_release_leaves_the_checkout_on_disk(self) -> None:
        acquire(self.worktree, holder="task:7", holder_session="session-A")
        self.run_cmd("release-occupancy", str(self.checkout))
        assert self.checkout.is_dir()
        assert Worktree.objects.filter(pk=self.worktree.pk).exists()


class PathResolutionTests(_CommandCase):
    """``_worktree_for_path`` — the operator types a path, not a row id."""

    def test_a_trailing_slash_still_resolves_to_the_row(self) -> None:
        assert _worktree_for_path(f"{self.checkout}/").pk == self.worktree.pk

    def test_an_unregistered_path_refuses_rather_than_guessing(self) -> None:
        with pytest.raises(SystemExit):
            _worktree_for_path("/nowhere/at/all")

    def test_an_exact_row_wins_over_an_earlier_alias_row(self) -> None:
        alias = f"/nonexistent-t3/alias/{self.checkout.name}"
        self.worktree.extra = {"worktree_path": alias}
        self.worktree.save(update_fields=["extra"])
        exact = WorktreeFactory(ticket=self.ticket, extra={"worktree_path": str(self.checkout)})
        with patched_mount_table(
            ("259:2", "/ws", str(self.checkout.parent)), ("259:2", "/ws", "/nonexistent-t3/alias")
        ):
            assert _worktree_for_path(str(self.checkout)).pk == exact.pk

    def test_a_resolved_row_wins_over_an_earlier_mount_alias(self) -> None:
        alias = f"/nonexistent-t3/alias/{self.checkout.name}"
        self.worktree.extra = {"worktree_path": alias}
        self.worktree.save(update_fields=["extra"])
        resolved = WorktreeFactory(ticket=self.ticket, extra={"worktree_path": str(self.checkout)})
        link = self.checkout.parent / f"{self.checkout.name}-link"
        link.symlink_to(self.checkout, target_is_directory=True)
        self.addCleanup(link.unlink)
        with patched_mount_table(
            ("259:2", "/ws", str(self.checkout.parent)), ("259:2", "/ws", "/nonexistent-t3/alias")
        ):
            assert _worktree_for_path(str(link)).pk == resolved.pk


class AttestTests(_CommandCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(lambda: shutil.rmtree(self.checkout, ignore_errors=True))

    def test_explicit_attest_records_dirty_checkout_state(self) -> None:
        _init_repo_with_branch(self.checkout, branch="feature", commits_ahead=1)
        (self.checkout / "new.txt").write_text("hand edit")
        state = checkout_state(self.checkout)
        with (
            patch("teatree.core.management.commands._worktree_occupancy.direct_owner_action", return_value=True),
            patch(
                "teatree.core.worktree.checkout_attest.read_process_table",
                return_value=ProcessTable(frozenset(), "/proc"),
                create=True,
            ),
        ):
            self.run_cmd("attest", str(self.checkout))
        assert self.fresh().get_extra()["attested_state"] == state.digest

    def test_agent_cannot_attest(self) -> None:
        _init_repo_with_branch(self.checkout, branch="feature", commits_ahead=1)
        err = StringIO()
        with (
            patch("teatree.core.management.commands._worktree_occupancy.direct_owner_action", return_value=False),
            pytest.raises(SystemExit) as refused,
        ):
            call_command("worktree", "attest", str(self.checkout), stderr=err)
        assert refused.value.code == 1
        assert "only a direct owner command may attest a checkout" in err.getvalue()
        assert "attested_state" not in self.fresh().get_extra()

    def test_changed_checkout_is_not_attested(self) -> None:
        _init_repo_with_branch(self.checkout, branch="feature", commits_ahead=1)
        state = checkout_state(self.checkout)
        changed = replace(state, digest="changed")
        with (
            patch("teatree.core.management.commands._worktree_occupancy.direct_owner_action", return_value=True),
            patch("teatree.core.worktree.checkout_attest.checkout_state", side_effect=[state, changed]),
            patch(
                "teatree.core.worktree.checkout_attest.read_process_table",
                return_value=ProcessTable(frozenset(), "/proc"),
            ),
            pytest.raises(SystemExit),
        ):
            self.run_cmd("attest", str(self.checkout))
        assert "attested_state" not in self.fresh().get_extra()

    def test_owner_attest_gives_one_claim_when_process_table_is_unavailable(self) -> None:
        _init_repo_with_branch(self.checkout, branch="feature", commits_ahead=1)
        state = checkout_state(self.checkout)
        unavailable = ProcessTable(frozenset(), "", ("host process table unavailable",))
        with (
            patch("teatree.core.management.commands._worktree_occupancy.direct_owner_action", return_value=True),
            patch("teatree.core.worktree.checkout_attest.read_process_table", return_value=unavailable),
        ):
            self.run_cmd("attest", str(self.checkout))
        assert self.fresh().get_extra()["attested_state"] == f"owner:{state.digest}"

    def test_explicit_attest_refuses_occupied_checkout(self) -> None:
        _init_repo_with_branch(self.checkout, branch="feature", commits_ahead=1)
        acquire(self.worktree, holder="task:7", holder_session="session-A")
        with (
            patch("teatree.core.management.commands._worktree_occupancy.direct_owner_action", return_value=True),
            pytest.raises(SystemExit),
        ):
            self.run_cmd("attest", str(self.checkout))
        assert "attested_state" not in self.fresh().get_extra()
