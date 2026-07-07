"""Forced-repro gate on ``ship()`` for FIX tickets (#118).

The gate refuses a FIX-ticket ship unless a provenance-verified RED->GREEN
``ReproEvidence`` pair exists (or a human-authorized ``ReproWaiver``). It is a
pure decision over durable state, DARK behind ``require_executed_repro``.

Symmetric corpus: must-BLOCK is a FIX ship with no/partial/invalid repro under
the flag; must-ALLOW is the flag OFF (dark no-op), a FEATURE ticket, a valid
pair, and a waiver. Each must-ALLOW is anti-vacuous against the same-shaped
must-BLOCK — the satisfier (or the flag) is what flips the verdict. The FSM
integration proves the pure verdict actually gates the real ``ship()``
transition and is load-bearing.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.gates import dod_gate
from teatree.core.gates.repro_gate import ForcedReproGateError, check_forced_repro
from teatree.core.models import ConfigSetting, HarnessRun, ReproEvidence, ReproWaiver, Ticket, Worktree
from tests.teatree_core.models._shared import _advance_ticket_to_tested, _complete_phase_task, _init_repo_with_branch

_SHA_RED = "a" * 40
_SHA_GREEN = "b" * 40
_CMD = "uv run pytest tests/x.py::test_bug"


def _enable_flag() -> None:
    ConfigSetting.objects.set_value("require_executed_repro", value=True)


def _fix_ticket(**kwargs: object) -> Ticket:
    return Ticket.objects.create(overlay="acme", kind=Ticket.Kind.FIX, **kwargs)


def _valid_pair(ticket: Ticket) -> None:
    ReproEvidence.record_red(ticket=ticket, command=_CMD, run=HarnessRun(head_sha=_SHA_RED, exit_code=1, output="boom"))
    ReproEvidence.record_green(
        ticket=ticket, command=_CMD, run=HarnessRun(head_sha=_SHA_GREEN, exit_code=0, output="ok"), red_is_ancestor=True
    )


class TestGateVerdict(TestCase):
    def test_flag_off_is_a_total_no_op_even_for_a_fix_with_no_evidence(self) -> None:
        # GREEN-8 (dark default): the load-bearing default-safe behaviour.
        check_forced_repro(_fix_ticket())  # does not raise

    def test_fix_with_no_evidence_under_flag_is_refused(self) -> None:
        # RED-1: a FIX ship with NO repro evidence and no waiver is refused.
        _enable_flag()
        with pytest.raises(ForcedReproGateError):
            check_forced_repro(_fix_ticket())

    def test_feature_ticket_is_never_gated(self) -> None:
        # GREEN-9: a FEATURE ticket is never gated, even under the flag.
        _enable_flag()
        check_forced_repro(Ticket.objects.create(overlay="acme", kind=Ticket.Kind.FEATURE))

    def test_valid_pair_passes(self) -> None:
        # GREEN-10: a provenance-verified RED->GREEN pair satisfies the gate.
        _enable_flag()
        ticket = _fix_ticket()
        _valid_pair(ticket)
        check_forced_repro(ticket)  # does not raise

    def test_red_only_is_refused(self) -> None:
        # RED-6: a partial (red-only) row was never shown to go green.
        _enable_flag()
        ticket = _fix_ticket()
        ReproEvidence.record_red(
            ticket=ticket, command=_CMD, run=HarnessRun(head_sha=_SHA_RED, exit_code=1, output="boom")
        )
        with pytest.raises(ForcedReproGateError):
            check_forced_repro(ticket)

    def test_hand_crafted_non_provenance_row_is_refused_at_the_gate(self) -> None:
        # RED-3 (gate layer): a directly-written row with both SHAs set but
        # provenance_ok=False must be rejected by the GATE itself, not only the
        # factory — the frozen ancestry proof is the gate's trust anchor.
        _enable_flag()
        ticket = _fix_ticket()
        ReproEvidence.objects.create(
            ticket=ticket,
            command=_CMD,
            command_fingerprint="deadbeef",
            red_head_sha=_SHA_RED,
            red_exit_code=1,
            red_output_digest="x",
            green_head_sha=_SHA_GREEN,
            green_exit_code=0,
            green_output_digest="y",
            provenance_ok=False,
        )
        with pytest.raises(ForcedReproGateError):
            check_forced_repro(ticket)

    def test_human_waiver_passes(self) -> None:
        # GREEN-11: a human-authorized waiver satisfies the gate with no evidence.
        _enable_flag()
        ticket = _fix_ticket()
        ReproWaiver.record(ticket=ticket, approver_id="souliane", reason="hardware-timing race, not determinizable")
        check_forced_repro(ticket)  # does not raise

    def test_deny_message_names_both_remedies(self) -> None:
        _enable_flag()
        with pytest.raises(ForcedReproGateError) as exc:
            check_forced_repro(_fix_ticket())
        message = str(exc.value)
        assert "record-red" in message
        assert "record-green" in message
        assert "waive" in message


class TestShipTransitionReproGate(TestCase):
    """The real FSM ``ship()`` path enforces the gate (#118)."""

    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _reviewed_fix_ticket(self) -> Ticket:
        ticket = Ticket.objects.create(kind=Ticket.Kind.FIX)
        repo_dir = self._tmp_path / f"repo-{ticket.pk}"
        branch = f"feature-{ticket.pk}"
        _init_repo_with_branch(repo_dir, branch=branch, commits_ahead=1)
        Worktree.objects.create(
            ticket=ticket,
            repo_path=str(repo_dir),
            branch=branch,
            extra={"worktree_path": str(repo_dir)},
        )
        _advance_ticket_to_tested(ticket)
        _complete_phase_task(ticket, "reviewing")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED
        return ticket

    def test_ship_refused_for_fix_without_repro_under_flag(self) -> None:
        # RED-1 (FSM): the block rolls back — ticket stays REVIEWED.
        _enable_flag()
        ticket = self._reviewed_fix_ticket()
        with patch.object(dod_gate, "frontend_repos_for_overlay", return_value=[]), pytest.raises(ForcedReproGateError):
            ticket.ship()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED

    def test_ship_proceeds_when_flag_off(self) -> None:
        # GREEN-8 (FSM): the dark default is a total no-op — the same FIX ship advances.
        ticket = self._reviewed_fix_ticket()
        with (
            patch.object(dod_gate, "frontend_repos_for_overlay", return_value=[]),
            self.captureOnCommitCallbacks(execute=False),
        ):
            ticket.ship()
            ticket.save()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED

    def test_ship_proceeds_with_valid_repro_under_flag(self) -> None:
        # GREEN-10 (FSM): a provenance-verified pair lets the FIX ship.
        _enable_flag()
        ticket = self._reviewed_fix_ticket()
        _valid_pair(ticket)
        with (
            patch.object(dod_gate, "frontend_repos_for_overlay", return_value=[]),
            self.captureOnCommitCallbacks(execute=False),
        ):
            ticket.ship()
            ticket.save()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
