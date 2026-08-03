"""The operator surface over a checkout's occupancy claim (souliane/teatree#3952).

The dispatched lane holds its claim for the length of a run; a hand-driven lane —
an operator, or an agent working a branch at the raw-git level — has no run to
scope it to, so it reaches the same claim through these commands. Every assertion
here is on the CLI's ANSWER (who holds it, what was refused, what was freed),
because that answer is the whole point: a second actor must be told the holder
rather than pointed into the tree.
"""

import tempfile
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Worktree
from teatree.core.worktree.occupancy import acquire, occupancy_holder
from tests.factories import TicketFactory, WorktreeFactory


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
