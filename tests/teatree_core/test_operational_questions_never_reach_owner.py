"""Every question producer outside the owner pin is the factory's own: INTERNAL, never mirrored to the owner (#5096).

The inventory is the conformance walk of ``tests/conformance/test_owner_decision_allowlist.py``: each
``DeferredQuestion.record(`` call naming no ``OwnerDecision`` and each ``ask_mr_state(`` call naming no
``owner`` must be classified below, so a new producer is a reviewed edit here, never a silent DM.
"""

import ast
from collections.abc import Callable

from django.test import TestCase

from teatree.core.models import DeferredQuestion
from teatree.core.review.mr_state_question import ask_mr_state
from tests.conformance.test_owner_decision_allowlist import _calls, _kind
from tests.factories import TaskFactory

_OPERATIONAL_PRODUCERS: dict[tuple[str, str], str] = {
    ("hooks/scripts/hook_router.py", "_capture_and_defer_question"): "a question a headless session was denied",
    ("src/teatree/agents/reactive_envelope_recorders.py", "_maybe_record_article_suggestions"): "the news batch",
    ("src/teatree/agents/review_envelope_recorder.py", "_latch_checks_contradiction"): "a spent review retry",
    ("src/teatree/core/models/task_phase_disposition.py", "_record_hold_cap"): "the self-review hold cap",
    ("src/teatree/core/models/task_phase_disposition.py", "record_stuck_transition_question"): "an FSM wedge",
    ("src/teatree/core/models/task_repair.py", "_escalate_cap"): "the repair iteration cap",
    ("src/teatree/core/models/task_repair.py", "_escalate_reoffers"): "the re-offer budget",
    ("src/teatree/core/models/task_repair.py", "_escalate_stall"): "a repair-loop stall",
    ("src/teatree/core/provision/failure_question.py", "record_provision_failure_question"): "a provision failure",
    ("src/teatree/core/tasks.py", "_record_attachment_hold_question"): "an attachment hold",
    ("src/teatree/loop/ci_eval_heal_advance.py", "_escalate_via_deferred_question"): "a halted CI-eval heal",
    ("src/teatree/loop/persistence_reviewer.py", "_escalate_unrecordable_review"): "an unrecordable review",
    ("src/teatree/loop/scanners/board_reconcile_issue_reopen.py", "_escalate_revival_cap_once"): "the revival cap",
    ("src/teatree/loop/scanners/memory_skim.py", "scan"): "the weekly memory skim",
    ("src/teatree/loop/stuck_ticket_redispatch.py", "_escalate_once"): "a halted stuck-ticket redispatch",
    ("src/teatree/loop/transient_requeue.py", "_escalate_once"): "a halted failed task",
    ("src/teatree/loops/directive_loop/revert.py", "ask_revert"): "a directive revert",
    ("src/teatree/loops/outer_loop/keep.py", "ask_keep"): "an outer-loop keep",
    ("src/teatree/loops/outer_loop/ratify.py", "_undecidable_answer_question"): "an outer-loop re-ask",
    ("src/teatree/loops/outer_loop/ratify.py", "ask_ratification"): "an outer-loop ratification",
    ("src/teatree/loops/outer_loop/revert.py", "ask_revert"): "an outer-loop revert",
    ("src/teatree/loops/outer_loop/tick.py", "_park_converged"): "the outer-loop convergence brake",
    ("src/teatree/loops/timer_chains.py", "_escalate_tick_timeout"): "a killed loop tick",
    ("src/teatree/core/gates/review_request_batch_gate.py", "work_group_ready"): "a merge request's state",
    ("src/teatree/loop/scanners/mr_triage_scan.py", "scan"): "a merge request's state",
    ("src/teatree/loop/scanners/review_nag.py", "_ask_owner_for_state"): "a merge request's state",
}


def _record(**fields: object) -> DeferredQuestion:
    return DeferredQuestion.record(str(fields.pop("question", "an operational question")), **fields)


type Site = tuple[Callable[..., DeferredQuestion | None], str, str, ast.Call]


def _operational_sites() -> list[Site]:
    records: list[Site] = [(_record, *site) for site in _calls("record") if _kind(site[2]) is None]
    asks: list[Site] = [
        (ask_mr_state, *site)
        for site in _calls("ask_mr_state")
        if not any(keyword.arg == "owner" for keyword in site[2].keywords)
    ]
    return records + asks


class OperationalQuestionsNeverReachOwnerTests(TestCase):
    def test_every_operational_producer_is_classified(self) -> None:
        assert {(path, function) for _producer, path, function, _call in _operational_sites()} == set(
            _OPERATIONAL_PRODUCERS
        )

    def test_no_operational_producer_can_pass_a_decision_through_a_splat(self) -> None:
        splats = [
            (path, function)
            for _producer, path, function, call in _operational_sites()
            if any(isinstance(arg, ast.Starred) for arg in call.args)
            or any(keyword.arg is None for keyword in call.keywords)
        ]
        assert splats == []

    def test_each_producer_call_shape_records_a_row_the_owner_never_sees(self) -> None:
        task = TaskFactory()
        for n, (producer, path, function, call) in enumerate(_operational_sites()):
            values = {
                "question": f"operational question {n}",
                "options_json": '["fix", "ignore"]',
                "session_id": "operational-session",
                "tool_use_id": f"toolu_{n}",
                "options_hash": f"operational:{n}",
                "generation": 1,
                "run_id": "run",
                "dedupe_marker": f"operational:{n}",
                "parked_task": task,
                "task_session": task.session,
                "mr_url": f"https://github.com/souliane/teatree/pull/{n + 1}",
                "reason": "the checks are red",
                "options": ("merge", "wait"),
                "head_sha": f"{n + 1:040x}",
            }
            with self.subTest(path=path, function=function):
                row = producer(**{keyword.arg: values[keyword.arg] for keyword in call.keywords if keyword.arg})
                assert row is not None
                assert row.audience == DeferredQuestion.Audience.INTERNAL
                assert not DeferredQuestion.owner_pending().filter(pk=row.pk).exists()
                assert not DeferredQuestion.unmirrored_pending().filter(pk=row.pk).exists()
