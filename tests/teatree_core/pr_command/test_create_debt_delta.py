"""``pr create`` refuses a ship that introduces net-new tech debt (north-star PR-3).

The ``debt_delta_gate`` is wired into ``_run_ship_gates`` right after the PR-budget
gate (both cheap, before the expensive diff-rendering gates): ``run_debt_delta_gate``
diffs merge-base..HEAD and delegates to ``check_debt_delta``. Anti-vacuous at the
seam and flag-gated: the SAME wired adapter blocks a net-new ``# noqa`` when
``require_debt_delta`` is ON and is a no-op when it is OFF (the DARK default) — the
flag is what flips the outcome, and a manifest waiver lets a justified suppression
through.
"""

from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.gates import debt_delta_gate as debt_gate_mod
from teatree.core.management.commands import _ship_gates as ship_gates_mod
from teatree.core.management.commands._ship_gates import run_debt_delta_gate
from teatree.core.models import PlanArtifact, Ticket, Worktree
from teatree.utils.run import CommandFailedError

from ._shared import _MOCK_OVERLAY, _shippable_ticket

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)

_NEW_NOQA = (
    "diff --git a/src/teatree/m.py b/src/teatree/m.py\n"
    "--- a/src/teatree/m.py\n"
    "+++ b/src/teatree/m.py\n"
    "@@ -1,1 +1,2 @@\n"
    " unchanged = 1\n"
    "+risky = frobnicate()  # noqa: F821\n"
)
_CLEAN = (
    "diff --git a/src/teatree/m.py b/src/teatree/m.py\n"
    "--- a/src/teatree/m.py\n"
    "+++ b/src/teatree/m.py\n"
    "@@ -1,1 +1,2 @@\n"
    " unchanged = 1\n"
    "+clean = compute()\n"
)


def _enabled() -> UserSettings:
    return UserSettings(require_debt_delta=True)


def _disabled() -> UserSettings:
    return UserSettings(require_debt_delta=False)


class TestRunDebtDeltaGate(TestCase):
    """The ship-chain adapter: flag gate -> diff -> delegate -> typed failure or None."""

    def _worktree(self, ticket: Ticket) -> Worktree:
        return Worktree.objects.create(
            ticket=ticket,
            overlay=ticket.overlay,
            repo_path="/tmp/wt",
            branch="feat",
            extra={"worktree_path": "/tmp/wt"},
        )

    def test_blocks_net_new_debt_when_flag_on(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        worktree = self._worktree(ticket)
        with (
            patch.object(debt_gate_mod, "get_effective_settings", return_value=_enabled()),
            patch.object(debt_gate_mod.git, "branch_diff", return_value=_NEW_NOQA),
        ):
            result = run_debt_delta_gate(ticket, worktree)
        assert result is not None
        assert result["allowed"] is False
        assert "noqa" in result["error"]

    def test_inert_when_flag_off_even_with_net_new_debt(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        worktree = self._worktree(ticket)
        with (
            patch.object(debt_gate_mod, "get_effective_settings", return_value=_disabled()),
            patch.object(debt_gate_mod.git, "branch_diff", return_value=_NEW_NOQA),
        ):
            assert run_debt_delta_gate(ticket, worktree) is None

    def test_passes_a_clean_diff_when_flag_on(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        worktree = self._worktree(ticket)
        with (
            patch.object(debt_gate_mod, "get_effective_settings", return_value=_enabled()),
            patch.object(debt_gate_mod.git, "branch_diff", return_value=_CLEAN),
        ):
            assert run_debt_delta_gate(ticket, worktree) is None

    def test_passes_when_manifest_waives_the_debt(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        worktree = self._worktree(ticket)
        PlanArtifact.objects.create(
            ticket=ticket,
            plan_text="plan",
            recorded_by="tester",
            adequacy={"approved_debt": [{"pattern": "noqa: F821", "reason": "stub gap"}]},
        )
        with (
            patch.object(debt_gate_mod, "get_effective_settings", return_value=_enabled()),
            patch.object(debt_gate_mod.git, "branch_diff", return_value=_NEW_NOQA),
        ):
            assert run_debt_delta_gate(ticket, worktree) is None

    def test_no_op_when_diff_unresolvable(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        worktree = self._worktree(ticket)
        with (
            patch.object(debt_gate_mod, "get_effective_settings", return_value=_enabled()),
            patch.object(
                ship_gates_mod.git,
                "branch_diff",
                side_effect=CommandFailedError(["git", "diff"], 128, "", "not a git repo"),
            ),
        ):
            assert run_debt_delta_gate(ticket, worktree) is None


class TestPrCreateDebtDeltaWiring(TestCase):
    """End-to-end proof the gate is live in ``_run_ship_gates`` under ``pr create``."""

    def test_pr_create_blocks_a_ship_introducing_net_new_debt(self) -> None:
        ticket = _shippable_ticket()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(debt_gate_mod, "get_effective_settings", return_value=_enabled()),
            patch.object(debt_gate_mod.git, "branch_diff", return_value=_NEW_NOQA),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))
        ticket.refresh_from_db()
        assert result.get("allowed") is False
        assert "debt_delta_gate" in str(result.get("error"))
        assert ticket.state != Ticket.State.SHIPPED

    def test_pr_create_not_blocked_when_flag_off(self) -> None:
        # The DARK default: the wired gate never blocks a ship even with net-new debt.
        ticket = _shippable_ticket()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(debt_gate_mod, "get_effective_settings", return_value=_disabled()),
            patch.object(debt_gate_mod.git, "branch_diff", return_value=_NEW_NOQA),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))
        assert "debt_delta_gate" not in str(result.get("error", ""))
