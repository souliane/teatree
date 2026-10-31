"""The coordinator's action table holds exactly four kinds, and each is bounded by the owner's four limits (#5685).

Every kind needs a limit row, a processor and a duty row, so a fifth kind cannot arrive as a prompt: it is a
reviewed change to the table, and this test fails until each of the three exists. The model only proposes; the
checks below run on the code that disposes.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from unittest import mock

from django.test import TestCase

from teatree.core.answering.coordinator_actions import _PROCESSORS, PeerReceipt, run_coordinator_actions
from teatree.core.modelkit.coordinator_action import (
    ACTION_SPECS,
    ALLOWED_DESTINATIONS,
    ActionDestination,
    CoordinatorActionKind,
    card_refusal,
)
from teatree.core.modelkit.owner_decision import OWNER_KEPT_DECISIONS, OwnerDecision
from teatree.core.models import Session, Task, ThreadBinding, Ticket
from teatree.utils.url_slug import slack_conversation_anchor

_COORDINATING_SKILL = Path(__file__).resolve().parents[2] / "skills" / "coordinating" / "SKILL.md"
_APPROVED_KINDS = {"bind_thread", "queue_task", "settle_card", "message_peer"}
_WORK_URL = "https://github.com/souliane/teatree/issues/4084"
_NONCE = "nonce-7f3a91c2"


def _duty_headings() -> set[str]:
    return set(re.findall(r"^## (.+)$", _COORDINATING_SKILL.read_text(encoding="utf-8"), flags=re.MULTILINE))


def _gaps(
    specs: Mapping[CoordinatorActionKind, object] = ACTION_SPECS,
    processors: Mapping[CoordinatorActionKind, Callable[..., str]] = _PROCESSORS,
) -> list[str]:
    headings = _duty_headings()
    gaps = [f"{kind}: no limit row" for kind in CoordinatorActionKind if kind not in specs]
    gaps += [f"{kind}: no processor" for kind in CoordinatorActionKind if kind not in processors]
    gaps += [
        f"{kind}: no duty row naming a coordinating-skill section"
        for kind, spec in ACTION_SPECS.items()
        if spec.duty not in headings
    ]
    gaps += [f"{kind}: no brief wording" for kind, spec in ACTION_SPECS.items() if not spec.guide.strip()]
    return gaps


class TestTheTableHoldsExactlyTheFourApprovedKinds(TestCase):
    def test_no_kind_beyond_the_four_exists(self) -> None:
        assert {kind.value for kind in CoordinatorActionKind} == _APPROVED_KINDS
        assert {kind.value for kind in ACTION_SPECS} == _APPROVED_KINDS
        assert {kind.value for kind in _PROCESSORS} == _APPROVED_KINDS

    def test_every_kind_has_a_limit_row_a_processor_and_a_duty_row(self) -> None:
        assert _gaps() == []

    def test_a_kind_with_no_limit_row_is_reported(self) -> None:
        kind = CoordinatorActionKind.SETTLE_CARD
        without = {k: spec for k, spec in ACTION_SPECS.items() if k is not kind}

        assert f"{kind}: no limit row" in _gaps(specs=without)

    def test_a_kind_with_no_processor_is_reported(self) -> None:
        kind = CoordinatorActionKind.MESSAGE_PEER
        without = {k: processor for k, processor in _PROCESSORS.items() if k is not kind}

        assert f"{kind}: no processor" in _gaps(processors=without)

    def test_a_kind_whose_duty_names_no_coordinating_section_is_reported(self) -> None:
        kind = CoordinatorActionKind.QUEUE_TASK

        with mock.patch.dict(ACTION_SPECS, {kind: replace(ACTION_SPECS[kind], duty="Not a section")}):
            assert f"{kind}: no duty row naming a coordinating-skill section" in _gaps()

    def test_a_kind_with_no_brief_wording_is_reported(self) -> None:
        kind = CoordinatorActionKind.BIND_THREAD

        with mock.patch.dict(ACTION_SPECS, {kind: replace(ACTION_SPECS[kind], guide=" ")}):
            assert f"{kind}: no brief wording" in _gaps()


class TestNoKindCanCrossAnOwnerLimit(TestCase):
    def setUp(self) -> None:
        ticket = Ticket.objects.create(
            issue_url=slack_conversation_anchor(channel="D-owner", slack_ts="1.0"),
            overlay="t3-teatree",
            extra={"slack_answer": {"channel": "D-owner", "slack_ts": "1.0", "question": f"{_NONCE} {_WORK_URL}"}},
        )
        ThreadBinding.bind(ticket=ticket, channel="D-owner", thread_ts="1.0")
        self.session = Session.objects.create(ticket=ticket, overlay="t3-teatree", agent_id="answering")
        self.task = Task.objects.create(ticket=ticket, session=self.session, phase="answering")

    def _fresh_task(self) -> Task:
        return Task.objects.create(ticket=self.task.ticket, session=self.session, phase="answering")

    def _arguments(self, kind: CoordinatorActionKind) -> dict[str, str]:
        return dict.fromkeys(ACTION_SPECS[kind].arguments, "x")

    def _run_with_a_spy_processor(self, kind: CoordinatorActionKind, spec: object) -> tuple[mock.Mock, list[str]]:
        spy = mock.Mock(return_value="ran")
        messenger = mock.Mock(return_value=PeerReceipt(accepted=True, detail=""))
        with (
            mock.patch.dict(ACTION_SPECS, {kind: spec}),
            mock.patch.dict(_PROCESSORS, {kind: spy}),
        ):
            receipts = run_coordinator_actions(
                self._fresh_task(), [{"kind": kind.value, **self._arguments(kind)}], messenger=messenger
            )
        assert messenger.call_count == 0
        return spy, receipts

    def test_a_planted_row_crossing_any_kept_limit_is_refused_and_its_processor_never_runs(self) -> None:
        for kind in CoordinatorActionKind:
            for decision in sorted(OWNER_KEPT_DECISIONS):
                with self.subTest(kind=kind, decision=decision):
                    crossing = replace(ACTION_SPECS[kind], crosses=frozenset({decision}))

                    spy, receipts = self._run_with_a_spy_processor(kind, crossing)

                    assert spy.call_count == 0
                    assert receipts == [f"Could not run {kind}: {kind} would cross the owner's {decision} limit."]

    def test_a_planted_row_that_lands_on_a_colleague_is_refused_and_its_processor_never_runs(self) -> None:
        for kind in CoordinatorActionKind:
            with self.subTest(kind=kind):
                to_colleague = replace(ACTION_SPECS[kind], destination=ActionDestination.COLLEAGUE_MESSAGE)

                spy, receipts = self._run_with_a_spy_processor(kind, to_colleague)

                assert spy.call_count == 0
                assert "outside the owner's thread" in receipts[0]

    def test_no_declared_row_crosses_a_limit_or_lands_outside_the_owners_boundary(self) -> None:
        for kind, spec in ACTION_SPECS.items():
            with self.subTest(kind=kind):
                assert spec.crosses & OWNER_KEPT_DECISIONS == frozenset()
                assert spec.destination in ALLOWED_DESTINATIONS
        assert ActionDestination.COLLEAGUE_MESSAGE not in ALLOWED_DESTINATIONS

    def test_a_card_is_settled_only_when_it_decides_none_of_the_four_limits(self) -> None:
        assert {decision for decision in OwnerDecision if card_refusal(decision)} == OWNER_KEPT_DECISIONS
        assert card_refusal(None) != ""

    def test_a_proposal_that_names_nothing_is_refused_by_every_kind(self) -> None:
        for kind in CoordinatorActionKind:
            with self.subTest(kind=kind):
                receipts = run_coordinator_actions(self.task, [{"kind": kind.value}])

                assert len(receipts) == 1
                assert receipts[0].startswith(f"Could not run {kind}: ")

    def test_every_refusal_is_audited_once_per_task_so_a_later_task_is_judged_afresh(self) -> None:
        later = Task.objects.create(ticket=self.task.ticket, session=self.session, phase="answering")
        for kind in CoordinatorActionKind:
            with self.subTest(kind=kind):
                proposal = [{"kind": kind.value}]

                first = run_coordinator_actions(self.task, proposal)
                again = run_coordinator_actions(self.task, proposal)
                from_later_task = run_coordinator_actions(later, proposal)

                assert (len(first), again, len(from_later_task)) == (1, [], 1)

    def test_an_unexpected_failure_stops_the_entries_after_it_for_every_kind(self) -> None:
        for kind in CoordinatorActionKind:
            with self.subTest(kind=kind):
                with mock.patch.dict(_PROCESSORS, {kind: mock.Mock(side_effect=RuntimeError("down"))}):
                    receipts = run_coordinator_actions(self.task, [{"kind": kind.value}, {"kind": "no_such_kind"}])

                assert receipts == [f"Could not confirm {kind}; earlier confirmed actions are listed above."]


class TestQueueTaskBuildsItsReasonInCode(TestCase):
    def test_owner_text_never_reaches_the_task_the_planner_is_given(self) -> None:
        ticket = Ticket.objects.create(
            issue_url=slack_conversation_anchor(channel="D-owner", slack_ts="1.0"),
            overlay="t3-teatree",
            extra={"slack_answer": {"channel": "D-owner", "slack_ts": "1.0", "question": f"{_NONCE} please plan"}},
        )
        session = Session.objects.create(ticket=ticket, overlay="t3-teatree", agent_id="answering")
        task = Task.objects.create(ticket=ticket, session=session, phase="answering")

        run_coordinator_actions(task, [{"kind": "queue_task", "ticket": ticket.issue_url}])

        planning = Task.objects.get(ticket=ticket, phase="planning")
        assert _NONCE not in planning.execution_reason
        assert _NONCE not in planning.subject
        assert planning.execution_reason == "Plan this ticket from its recorded context."
