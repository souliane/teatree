"""``manage.py ticket sync-completions`` must survive a per-ticket gate refusal.

CFG-2: the whole-table completion sweep advances each post-ship ticket whose
upstream issue is done through :func:`_advance_ticket`. That FSM walk can be
*gate-refused* — an ``in_review`` ticket with no merged-SHA evidence raises
:class:`~teatree.core.gates.merge_evidence_gate.NoMergeEvidenceError` at
``mark_merged()``. The uncaught call aborted the ENTIRE sweep on the first such
ticket, so every later completable ticket was silently skipped. The sweep must
catch a per-ticket failure, record it, and CONTINUE — reporting the refusals at
the end (mirroring ``reconcile_overlay``).
"""

from typing import cast
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands._sweep_commands import CompletionResult, _completion_line
from teatree.core.models import ConfigSetting, Ticket

type _Completion = dict[str, object]


class _AlwaysDoneOverlay:
    """A stand-in overlay whose upstream issue is always ``done``."""

    def is_issue_done(self, _issue_data: dict[str, object]) -> bool:
        return True


class _FakeHost:
    """A code host whose issue fetch always succeeds with a non-error payload."""

    def get_issue(self, _issue_url: str) -> dict[str, object]:
        return {"state": "closed"}


class TestSyncCompletionsSurvivesGateRefusal(TestCase):
    """A gate-refused first ticket must not abort the sweep for the rest."""

    def setUp(self) -> None:
        # The merge-evidence gate must bite so the ``in_review`` ticket is genuinely refused.
        ConfigSetting.objects.set_value("require_merge_evidence", value=True)

    def test_first_ticket_gate_refused_still_advances_the_second(self) -> None:
        # Created first → lower pk → iterated first. It has no merge evidence, so
        # `mark_merged()` refuses. Before the fix its uncaught raise aborted the sweep.
        refused = Ticket.objects.create(
            overlay="fake-overlay",
            state=Ticket.State.IN_REVIEW,
            issue_url="https://gitlab.com/acme/widgets/-/issues/1",
        )
        # A `merged` ticket only walks `retrospect()` (no gate) → advances cleanly.
        advancing = Ticket.objects.create(
            overlay="fake-overlay",
            state=Ticket.State.MERGED,
            issue_url="https://gitlab.com/acme/widgets/-/issues/2",
        )

        with (
            patch(
                "teatree.core.management.commands._sweep_commands.get_all_overlays",
                return_value={"fake-overlay": _AlwaysDoneOverlay()},
            ),
            patch(
                "teatree.core.management.commands._sweep_commands.get_code_host_for_url",
                return_value=_FakeHost(),
            ),
        ):
            results = cast("list[_Completion]", call_command("ticket", "sync-completions"))

        refused.refresh_from_db()
        advancing.refresh_from_db()
        # The sweep CONTINUED past the refusal: the second ticket advanced.
        assert advancing.state == Ticket.State.RETROSPECTED
        # The refused ticket was NOT advanced and is reported, not swallowed.
        assert refused.state == Ticket.State.IN_REVIEW
        refused_rows = [r for r in results if r["action"] == "refused"]
        assert [r["ticket_id"] for r in refused_rows] == [refused.pk]
        assert "merged-SHA evidence" in cast("str", refused_rows[0]["error"])
        completed_rows = [r for r in results if r["action"] == "completed"]
        assert [r["ticket_id"] for r in completed_rows] == [advancing.pk]

    def test_mid_chain_refusal_reports_the_persisted_partial_state(self) -> None:
        # A ``shipped`` ticket walks ``request_review()`` (ungated → commits, so it
        # lands at ``in_review``) then ``mark_merged()`` (merge-evidence gate refuses).
        # The refusal must report the persisted ``in_review`` landing, not the stale
        # ``shipped`` starting state, so the operator sees the partial progress.
        partial = Ticket.objects.create(
            overlay="fake-overlay",
            state=Ticket.State.SHIPPED,
            issue_url="https://gitlab.com/acme/widgets/-/issues/3",
        )

        with (
            patch(
                "teatree.core.management.commands._sweep_commands.get_all_overlays",
                return_value={"fake-overlay": _AlwaysDoneOverlay()},
            ),
            patch(
                "teatree.core.management.commands._sweep_commands.get_code_host_for_url",
                return_value=_FakeHost(),
            ),
        ):
            results = cast("list[_Completion]", call_command("ticket", "sync-completions"))

        partial.refresh_from_db()
        assert partial.state == Ticket.State.IN_REVIEW  # the first transition persisted
        refused_rows = [r for r in results if r["action"] == "refused"]
        assert [r["ticket_id"] for r in refused_rows] == [partial.pk]
        assert refused_rows[0]["from_state"] == "shipped"
        assert refused_rows[0]["to_state"] == "in_review"

    def test_completion_line_shows_partial_state_only_when_it_diverges(self) -> None:
        diverged = CompletionResult(
            ticket_id=7, issue_url="u", from_state="shipped", to_state="in_review", action="refused", error="boom"
        )
        assert _completion_line(diverged) == "  #7 shipped → in_review refused: boom"
        stalled = CompletionResult(
            ticket_id=8, issue_url="u", from_state="in_review", to_state="in_review", action="refused", error="boom"
        )
        assert _completion_line(stalled) == "  #8 in_review → refused: boom"
