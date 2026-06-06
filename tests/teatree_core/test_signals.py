from collections.abc import Callable
from contextlib import AbstractContextManager
from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase, override_settings

import teatree.core.overlay_loader as overlay_loader_mod
import teatree.core.signals as signals_mod
from teatree.core.models import PullRequest, Session, Task, Ticket
from teatree.core.models.transition import TicketTransition
from tests.teatree_core._on_behalf_gate_helpers import on_behalf_gate_off
from tests.teatree_core.conftest import CommandOverlay


class _FakeReactionPublisher:
    """Test double for the reaction-publisher registry — routes one method to *fn*."""

    def __init__(self, *, transition: Callable[..., int] | None = None, approval: Callable[..., int] | None = None):
        self._transition = transition or (lambda *_a, **_k: 0)
        self._approval = approval or (lambda *_a, **_k: 0)

    def add_reactions_for_transition(self, ticket: object, transition_name: str) -> int:
        return self._transition(ticket, transition_name)

    def add_approval_reaction(self, pull_request: object) -> int:
        return self._approval(pull_request)


def _patch_transition_publisher(fn: Callable[..., int]) -> AbstractContextManager[object]:
    return patch.object(signals_mod, "get_reaction_publisher", lambda: _FakeReactionPublisher(transition=fn))


def _patch_approval_publisher(fn: Callable[..., int]) -> AbstractContextManager[object]:
    return patch.object(signals_mod, "get_reaction_publisher", lambda: _FakeReactionPublisher(approval=fn))


IMMEDIATE_BACKEND = {
    "TASKS": {
        "default": {
            "BACKEND": "django_tasks.backends.immediate.ImmediateBackend",
        },
    },
}

_MOCK_OVERLAY = {"test": CommandOverlay()}


class TestAutoEnqueueHeadlessSignal(TestCase):
    """post_save signal auto-enqueues headless tasks on creation."""

    @override_settings(**IMMEDIATE_BACKEND)
    def test_headless_task_auto_executes_on_creation(self) -> None:
        import json as _json  # noqa: PLC0415
        import shlex  # noqa: PLC0415

        import teatree.agents.headless as headless_mod  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")

        # ``architectural_review`` has no registered phase agent, so it is NOT
        # loop-dispatched and the post_save signal owns its execution. A
        # loop-dispatched phase (coding/testing/...) is the loop's
        # responsibility and is intentionally not auto-enqueued.
        result_blob = _json.dumps({"summary": "OK"})
        with (
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude-code"),
            patch.object(
                headless_mod,
                "_build_headless_command",
                return_value=["sh", "-c", f"printf %s {shlex.quote(result_blob)}"],
            ),
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
        ):
            task = Task.objects.create(
                ticket=ticket,
                session=session,
                execution_target=Task.ExecutionTarget.HEADLESS,
                phase="architectural_review",
            )

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_interactive_task_not_enqueued(self) -> None:
        """Interactive tasks are not auto-enqueued by the signal."""
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")

        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            phase="coding",
        )

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_non_pending_headless_task_not_enqueued(self) -> None:
        """Already-completed headless tasks are not re-enqueued."""
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")

        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.COMPLETED,
        )

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_signal_failure_leaves_task_pending(self) -> None:
        """If enqueue fails, the task remains PENDING for drain to retry."""
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")

        import teatree.core.tasks as tasks_mod  # noqa: PLC0415

        class BrokenEnqueue:
            @staticmethod
            def enqueue(*_args: object, **_kwargs: object) -> None:
                msg = "backend unavailable"
                raise RuntimeError(msg)

        # ``architectural_review`` is NOT loop-dispatched, so the auto-enqueue
        # actually fires and hits the broken backend (a loop-dispatched phase
        # would skip the enqueue entirely and never exercise this path).
        with patch.object(tasks_mod, "execute_headless_task", BrokenEnqueue):
            task = Task.objects.create(
                ticket=ticket,
                session=session,
                execution_target=Task.ExecutionTarget.HEADLESS,
                phase="architectural_review",
            )

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    @override_settings(**IMMEDIATE_BACKEND)
    def test_route_to_headless_triggers_enqueue(self) -> None:
        """Re-routing an interactive task to headless triggers auto-enqueue."""
        import json as _json  # noqa: PLC0415
        import shlex  # noqa: PLC0415

        import teatree.agents.headless as headless_mod  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")

        # ``architectural_review`` has no registered phase agent, so it is NOT
        # loop-dispatched — the post_save auto-enqueue owns its execution.
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            phase="architectural_review",
        )
        assert task.status == Task.Status.PENDING

        result_blob = _json.dumps({"summary": "OK"})
        with (
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude-code"),
            patch.object(
                headless_mod,
                "_build_headless_command",
                return_value=["sh", "-c", f"printf %s {shlex.quote(result_blob)}"],
            ),
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
        ):
            task.route_to_headless(reason="Auto-rerouted for testing")

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED


class TestSlackReactionsOnTransition(TestCase):
    """post_transition signal triggers Slack reactions via the overlay config.

    These tests exercise the reaction-mechanics path; the on-behalf gate
    has its own dedicated suite (:class:`TestTransitionReactionGated`),
    so the gate is disabled inside each test.
    """

    def _ticket_with_mr(self) -> Ticket:
        return Ticket.objects.create(
            overlay="test",
            state=Ticket.State.IN_REVIEW,
            extra={
                "mrs": {
                    "https://gitlab.com/org/repo/-/merge_requests/1": {
                        "review_permalink": "https://team.slack.com/archives/C999/p1700000000000100",
                    }
                }
            },
        )

    def test_mark_merged_invokes_reactions(self) -> None:
        ticket = self._ticket_with_mr()
        called: list[tuple[object, str]] = []

        def _fake(t: object, name: str) -> int:
            called.append((t, name))
            return 1

        with on_behalf_gate_off(), _patch_transition_publisher(_fake):
            ticket.mark_merged()
            ticket.save()

        assert len(called) == 1
        assert called[0][1] == "mark_merged"

    def test_transition_survives_reaction_failure(self) -> None:
        ticket = self._ticket_with_mr()

        def _boom(*_a: object, **_kw: object) -> int:
            msg = "slack down"
            raise RuntimeError(msg)

        with on_behalf_gate_off(), _patch_transition_publisher(_boom):
            ticket.mark_merged()
            ticket.save()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_different_transitions_forward_their_name(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED, extra={"mrs": {}})
        names: list[str] = []

        def _record(_ticket: object, name: str) -> int:
            names.append(name)
            return 0

        with on_behalf_gate_off(), _patch_transition_publisher(_record):
            ticket.rework()
            ticket.save()

        assert names == ["rework"]

    def test_transition_commits_when_no_publisher_registered(self) -> None:
        """Fail-SAFE: an empty reaction registry → no-op reaction, transition still commits."""
        from teatree.core import reaction_dispatch  # noqa: PLC0415

        ticket = self._ticket_with_mr()
        original = reaction_dispatch._publisher
        reaction_dispatch._publisher = None
        try:
            with on_behalf_gate_off():
                ticket.mark_merged()
                ticket.save()
        finally:
            reaction_dispatch.register_reaction_publisher(original)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED


class TestApprovalReactionOnTransition(TestCase):
    """PullRequest.approve() posts a ✅ on the requester's review message (#961).

    The reaction is itself a post-on-behalf and routes through the same
    recorded-approval gate every other on-behalf post uses — it is
    satisfiable (the user records an :class:`OnBehalfApproval` scoped to
    the PR url + ``approval_reaction``), never pure suppression.
    """

    def _pr(self, slack_url: str = "https://team.slack.com/archives/C9/p1700000000000100") -> PullRequest:
        ticket = Ticket.objects.create(overlay="test")
        pr = PullRequest.objects.create(
            ticket=ticket,
            overlay="test",
            url="https://gitlab.com/org/repo/-/merge_requests/7",
            repo="org/repo",
            iid="7",
            state=PullRequest.State.OPEN,
        )
        pr.request_review(slack_url=slack_url)
        pr.save()
        return pr

    def test_approve_posts_check_mark_when_gate_off(self) -> None:
        pr = self._pr()
        calls: list[tuple[object,]] = []

        def _fake(pull_request: object) -> int:
            calls.append((pull_request,))
            return 1

        with on_behalf_gate_off(), _patch_approval_publisher(_fake):
            pr.approve()
            pr.save()

        assert len(calls) == 1
        assert calls[0][0] == pr

    def test_approve_skipped_when_gate_on_no_approval(self) -> None:
        pr = self._pr()
        calls: list[object] = []

        def _fake(pull_request: object) -> int:
            calls.append(pull_request)
            return 1

        # Gate ON (default) — no recorded approval → reaction is skipped
        # (NOT pure suppression: a recorded approval would let it
        # publish, exercised by the next test).
        with _patch_approval_publisher(_fake):
            pr.approve()
            pr.save()

        pr.refresh_from_db()
        assert pr.state == PullRequest.State.APPROVED
        assert calls == []

    def test_approve_posts_with_recorded_approval_when_gate_on(self) -> None:
        """Satisfiable: a recorded :class:`OnBehalfApproval` lets the reaction publish.

        Proves the gate is NOT pure suppression — the same gate-ON state
        that blocks the unapproved post lets the approved one through.
        """
        from teatree.core.models import OnBehalfApproval  # noqa: PLC0415

        pr = self._pr()
        OnBehalfApproval.record(target=pr.url, action="approval_reaction", approver_id="souliane")

        calls: list[object] = []

        def _fake(pull_request: object) -> int:
            calls.append(pull_request)
            return 1

        # Gate ON by default — but the recorded approval satisfies it.
        with _patch_approval_publisher(_fake):
            pr.approve()
            pr.save()

        assert calls == [pr]

    def test_approve_survives_reaction_failure(self) -> None:
        pr = self._pr()

        def _boom(_pull_request: object) -> int:
            msg = "slack down"
            raise RuntimeError(msg)

        with on_behalf_gate_off(), _patch_approval_publisher(_boom):
            pr.approve()
            pr.save()

        pr.refresh_from_db()
        assert pr.state == PullRequest.State.APPROVED

    def test_non_approve_transition_does_not_react(self) -> None:
        pr = self._pr()
        calls: list[object] = []

        with (
            on_behalf_gate_off(),
            _patch_approval_publisher(lambda p: calls.append(p) or 0),
        ):
            pr.mark_merged()
            pr.save()

        assert calls == []

    def test_add_approval_reaction_uses_white_check_mark_on_slack_url(self) -> None:
        """End-to-end: the real helper reacts on the PR's stored slack_url."""
        from teatree.backends import slack_reactions  # noqa: PLC0415

        pr = self._pr()
        recorded: list[tuple[str, str, str]] = []

        class _Cfg:
            @staticmethod
            def get_slack_token() -> str:
                return "xoxb-token"

        class _Overlay:
            config = _Cfg()

        def _fake_add_reaction(token: str, channel: str, ts: str, emoji: str) -> bool:
            recorded.append((channel, ts, emoji))
            return True

        with (
            patch.object(slack_reactions, "get_overlay", return_value=_Overlay()),
            patch.object(slack_reactions, "add_reaction", _fake_add_reaction),
        ):
            posted = slack_reactions.add_approval_reaction(pr)

        assert posted == 1
        assert recorded == [("C9", "1700000000.000100", "white_check_mark")]

    def test_ticket_without_mrs_is_noop(self) -> None:
        """The real handler is a silent no-op when the ticket has no MRs."""
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW, extra={})
        # No patching — the real code path must not raise (even with the
        # gate on, the transition itself must always succeed).
        with on_behalf_gate_off():
            ticket.mark_merged()
            ticket.save()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_approve_marks_review_assignment_rows_approved(self) -> None:
        """#1047: an approve transition closes every linked ``ReviewAssignment`` row.

        Closes the loop: reaction → review_intent dispatch → review →
        approve. The ledger row reaches its terminal state so the audit
        trail captures the full cycle.
        """
        from teatree.core.models import ReviewAssignment, ReviewIntent  # noqa: PLC0415

        pr = self._pr()
        row = ReviewAssignment.record(
            ReviewIntent(
                mr_url=pr.url,
                user_id="U0DEMOUSER1",
                channel="C9",
                slack_ts="1700000000.000100",
                trigger="reaction",
                overlay=pr.overlay,
            )
        )
        assert row is not None

        with on_behalf_gate_off(), _patch_approval_publisher(lambda _pr: 1):
            pr.approve()
            pr.save()

        row.refresh_from_db()
        assert row.state == ReviewAssignment.State.APPROVED
        assert row.approved_at is not None


class TestTransitionReactionGated(TestCase):
    """Ticket-transition Slack reactions are recorded-approval gated (#960).

    The reactions post emoji on the review-request Slack messages the
    user posted to colleagues earlier — a colleague-facing surface — so
    they route through the same recorded-approval gate every other
    on-behalf post uses (NOT pure suppression). The FSM transition
    itself is never blocked.
    """

    def _ticket(self) -> Ticket:
        return Ticket.objects.create(
            overlay="test",
            state=Ticket.State.IN_REVIEW,
            extra={"mrs": {"https://x/1": {"review_permalink": "https://t.slack.com/archives/C1/p1700000000000100"}}},
        )

    def test_transition_reaction_blocked_when_gate_on_no_approval(self) -> None:
        ticket = self._ticket()
        calls: list[tuple[object, str]] = []

        def _fake(t: object, name: str) -> int:
            calls.append((t, name))
            return 1

        # Gate ON by default — no recorded approval → reaction is skipped.
        with _patch_transition_publisher(_fake):
            ticket.mark_merged()
            ticket.save()

        ticket.refresh_from_db()
        # The FSM transition itself must NEVER be blocked.
        assert ticket.state == Ticket.State.MERGED
        assert calls == []

    def test_transition_reaction_proceeds_with_recorded_approval(self) -> None:
        """Satisfiable: a recorded approval lets the reaction publish even with the gate ON."""
        from teatree.core.models import OnBehalfApproval  # noqa: PLC0415

        ticket = self._ticket()
        OnBehalfApproval.record(
            target=f"ticket:{ticket.pk}",
            action="transition_reaction:mark_merged",
            approver_id="souliane",
        )

        calls: list[tuple[object, str]] = []

        def _fake(t: object, name: str) -> int:
            calls.append((t, name))
            return 1

        # Gate ON by default — recorded approval satisfies it.
        with _patch_transition_publisher(_fake):
            ticket.mark_merged()
            ticket.save()

        assert len(calls) == 1
        assert calls[0][1] == "mark_merged"


class TestLogTicketTransitionFaultIsolation(TestCase):
    """The transition-audit receiver must never break the FSM transition (#1882).

    ``_log_ticket_transition`` writes a ``TicketTransition`` audit row. A
    ``DatabaseError`` raised there must be swallowed and logged like every
    sibling receiver — the Ticket FSM transition itself completes and
    persists regardless.
    """

    def test_transition_survives_audit_db_error(self) -> None:
        ticket = Ticket.objects.create(overlay="test")

        with (
            patch.object(TicketTransition.objects, "create", side_effect=DatabaseError("audit table gone")),
            self.assertLogs(signals_mod.logger, level="ERROR"),
        ):
            ticket.scope()
            ticket.save()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SCOPED

    def test_audit_db_error_is_logged(self) -> None:
        ticket = Ticket.objects.create(overlay="test")

        with (
            patch.object(TicketTransition.objects, "create", side_effect=DatabaseError("audit table gone")),
            self.assertLogs(signals_mod.logger, level="ERROR") as captured,
        ):
            ticket.scope()
            ticket.save()

        assert any("audit table gone" in line for line in captured.output)
