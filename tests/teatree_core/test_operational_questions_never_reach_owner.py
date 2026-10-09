"""Every question producer outside the owner pin is the factory's own: INTERNAL, never mirrored to the owner (#5096).

The inventory is the conformance walk of ``tests/conformance/test_owner_decision_allowlist.py``: each
``DeferredQuestion.record(`` call naming no ``OwnerDecision`` and each ``ask_mr_state(`` call naming no
``owner`` maps to a driver below that reaches it through its public entry point, so a new producer is a
reviewed edit here, never a silent DM.
"""

import ast
import contextlib
import datetime as dt
import functools
import inspect
import io
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

import hooks.scripts.hook_router as router
from teatree.agents import review_envelope_recorder
from teatree.agents.attempt_recorder import record_result_envelope
from teatree.config import cold_reader
from teatree.core import notify as notify_module
from teatree.core.gates.review_request_batch_gate import WORK_GROUP_MAX_MEMBERS, work_group_ready
from teatree.core.gates.review_request_guard import GuardTarget, ReconcileResult, ReconcileStatus
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.models import DeferredQuestion, OuterLoopExperiment, Session, Task, TaskAttempt, Ticket
from teatree.core.models.loop import Loop
from teatree.core.models.task_phase_disposition import record_stuck_transition_question
from teatree.core.notify_question_drains import drain_unmirrored_deferred_questions
from teatree.core.repair_loop import MAX_REOFFERS, IterationStalled, MaxIterationsExceeded, max_phase_iterations
from teatree.core.runners.base import RunnerResult
from teatree.core.tasks import execute_provision
from teatree.loop.ci_eval_heal_advance import advance_open_sessions
from teatree.loop.dispatch import DispatchAction
from teatree.loop.persistence import persist_agent_actions
from teatree.loop.scanners import board_reconcile_issue_reopen as issue_reopen
from teatree.loop.scanners.board_reconcile import reconcile_board
from teatree.loop.scanners.memory_skim import MemorySkimScanner
from teatree.loop.scanners.mr_triage_scan import MrTriageScanner
from teatree.loop.scanners.review_nag import ReviewNagScanner
from teatree.loop.stuck_ticket_redispatch import redispatch_stuck_tickets
from teatree.loop.transient_requeue import requeue_transient_failed
from teatree.loops import timer_chains
from teatree.loops.directive_loop.revert import ask_revert as ask_directive_revert
from teatree.loops.outer_loop.keep import ask_keep
from teatree.loops.outer_loop.ratify import ask_ratification, try_admit
from teatree.loops.outer_loop.revert import ask_revert as ask_experiment_revert
from teatree.loops.outer_loop.tick import run_tick
from teatree.verification.url_check import UrlCheckResult, UrlCheckStatus
from tests.conformance._src_tree import REPO_ROOT
from tests.conformance.test_owner_decision_allowlist import _calls, _kind
from tests.factories import planned_ticket
from tests.teatree_agents.test_runner_review_verdict import _LIVE_RED, _contradiction_envelope, _exhaust_dispatch
from tests.teatree_core._self_review_helpers import author_ticket, completed_self_review, head
from tests.teatree_core.gates.test_review_request_batch_gate import _TICKET, _forge
from tests.teatree_core.gates.test_review_request_batch_gate import _Host as _ListingHost
from tests.teatree_core.gates.test_review_request_batch_gate import _mr as _listed_mr
from tests.teatree_core.gates.test_review_request_batch_gate import _url as _listed_url
from tests.teatree_loop._board_reconcile_overlays import rule_f_inert
from tests.teatree_loop.scanners.test_memory_skim_scanner import _entry
from tests.teatree_loop.scanners.test_mr_triage_scan import _SCOPE, _channel, _opened, _reads
from tests.teatree_loop.test_ci_eval_heal_advance import _artifact, _awaiting, _completed, _FakeClient
from tests.teatree_loop.test_review_nag_scanner import FakeHost, FakeSlack, _seed
from tests.teatree_loop.test_scanners import FakeCodeHost
from tests.teatree_loop.test_stuck_ticket_redispatch import _stuck_ticket
from tests.teatree_loops.directive_loop.test_revert import _revert_pending as _directive_revert_pending
from tests.teatree_loops.outer_loop.test_keep import _keep_pending
from tests.teatree_loops.outer_loop.test_ratify import _make_experiment
from tests.teatree_loops.outer_loop.test_revert import _revert_pending as _experiment_revert_pending
from tests.teatree_loops.outer_loop.test_tick import _seams

type Site = tuple[str, str]

_IMMEDIATE_TASKS = {"TASKS": {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}}


def _task(phase: str, *, ticket: Ticket | None = None, **fields: object) -> Task:
    owner = ticket if ticket is not None else Ticket.objects.create()
    return Task.objects.create(
        ticket=owner, session=Session.objects.create(ticket=owner, agent_id=phase), phase=phase, **fields
    )


def _failed_phase_task(*errors: str, issue: int) -> Task:
    task = _task("coding", ticket=Ticket.objects.create(issue_url=f"https://example.com/issues/{issue}"))
    for error in errors:
        TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=1, error=error)
    return task


def _provision(*, result: RunnerResult, ticket_text: list[str]) -> None:
    ticket = Ticket.objects.create(
        overlay="test", repos=["repo-a"], extra={"branch": "ac-repo-a-1-x"}, state=Ticket.State.WORK_STARTED
    )
    with (
        override_settings(**_IMMEDIATE_TASKS),
        patch("teatree.core.tasks.WorktreeProvisioner") as provisioner,
        patch("teatree.core.tasks.ticket_text_sources", return_value=ticket_text),
    ):
        provisioner.return_value.run.return_value = result
        execute_provision.enqueue(ticket.pk)


def _revive_past_the_reopen_cap() -> None:
    url = "https://github.com/acme/app/issues/4133"
    Ticket.objects.create(
        overlay="t3-teatree",
        state=Ticket.State.DELIVERED,
        issue_url=url,
        extra={"reopen_revivals": issue_reopen.MAX_REOPEN_REVIVALS},
    )
    with patch.object(issue_reopen, "_reopened_issue_urls", return_value=({url}, 1)), rule_f_inert():
        reconcile_board()


def _deny_a_loop_driven_ask() -> None:
    payload = {
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "Ship it?", "options": []}]},
        "session_id": "s-loop",
        "tool_use_id": "t-loop",
    }
    with (
        tempfile.TemporaryDirectory() as state_dir,
        patch.object(router, "_session_drives_loop", return_value=True),
        patch.object(router, "STATE_DIR", Path(state_dir)),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        router.handle_mirror_question_to_slack(payload)


def _record_a_news_batch() -> None:
    task = _task("scanning_news", ticket=Ticket.objects.create(state=Ticket.State.WORK_STARTED, overlay="acme"))
    task.claim(claimed_by="loop-slot")
    reachable = UrlCheckResult(url="", status=UrlCheckStatus.OK, http_status=200)
    suggestion = {"title": "Agent evals", "url": "https://example.com/a", "rationale": "why"}
    with patch("teatree.core.models.pending_article_suggestion.check_url", return_value=reachable):
        record_result_envelope(task, {"summary": "1 candidate", "article_suggestions": [suggestion]})


def _latch_a_spent_review_head() -> None:
    task = _exhaust_dispatch().task
    assert task is not None
    task.claim(claimed_by="headless-reviewer")
    with patch.object(review_envelope_recorder, "live_checks_at", _LIVE_RED):
        record_result_envelope(task, _contradiction_envelope(), phase="reviewing")


def _hold_past_the_rework_cap() -> None:
    ticket = author_ticket()
    for index in range(max_phase_iterations()):
        completed_self_review(ticket, "hold", reviewed_sha=head(index))
    latest = completed_self_review(ticket, "hold", reviewed_sha=head(999))
    with patch.object(Ticket, "has_shippable_diff", return_value=True):
        latest._apply_phase_transition()


def _wedge_a_finished_phase() -> None:
    record_stuck_transition_question(None, phase="retro", ticket=Ticket.objects.create(), refusal="no transition")


def _spend_the_reoffer_budget() -> None:
    expired = timezone.now() - dt.timedelta(minutes=10)
    _task(
        "coding",
        ticket=Ticket.objects.create(issue_url="https://example.com/issues/3"),
        status=Task.Status.CLAIMED,
        claimed_by="worker",
        claimed_at=expired,
        lease_expires_at=expired,
        reclaim_count=MAX_REOFFERS,
    )
    Task.objects.reclaim_orphaned_claims()


def _stall_a_repair_loop() -> None:
    with contextlib.suppress(IterationStalled):
        _failed_phase_task("the identical failure", "the identical failure", issue=1).check_requeue_allowed()


def _spend_the_repair_cap() -> None:
    with contextlib.suppress(MaxIterationsExceeded):
        _failed_phase_task(
            *(f"failed {'x' * n}" for n in range(max_phase_iterations())), issue=2
        ).check_requeue_allowed()


def _fail_a_provision() -> None:
    _provision(result=RunnerResult(ok=False, detail="git clone failed"), ticket_text=[])


def _hold_an_unfetched_attachment() -> None:
    attachment = "/uploads/" + "a" * 32 + "/spec.pdf"
    _provision(result=RunnerResult(ok=True, detail="provisioned 1 worktree(s)"), ticket_text=[f"spec {attachment}"])


def _halt_a_ci_eval_heal() -> None:
    session = _awaiting()
    session.fix_attempts = session.max_fix_attempts
    session.save(update_fields=["fix_attempts"])
    advance_open_sessions(
        client=_FakeClient(runs=[_completed("failure")], artifact=_artifact(reds=["rules_under_load"]))
    )


def _refuse_an_unrecordable_review() -> None:
    url = "https://example.com/owner/repo/pull/4225"
    payload = {"url": url, "head_sha": "", "previous_sha": "", "overlay": "acme"}
    persist_agent_actions([DispatchAction(kind="agent", zone="t3:reviewer", detail=f"Review: {url}", payload=payload)])


def _skim_promotable_memories() -> None:
    with patch("teatree.memory_audit.scan_all", return_value=[_entry("never-force-push")]):
        MemorySkimScanner().scan()


def _halt_a_stuck_ticket() -> None:
    ticket = _stuck_ticket()
    for n in range(max_phase_iterations()):
        task = _task("planning", ticket=ticket, status=Task.Status.FAILED)
        attempt = TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=1, error=f"run {'x' * n}")
        TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=timezone.now() - dt.timedelta(hours=48))
    redispatch_stuck_tickets()


def _halt_a_failed_task() -> None:
    error = "AssertionError: widget count was 3, expected 4"
    task = _task("reviewing", ticket=planned_ticket(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED))
    task.fail(reason=error, by_holder=False)
    TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=1, error=error)
    requeue_transient_failed()


def _proposed_experiment() -> OuterLoopExperiment:
    return _make_experiment(
        hypothesis="H", target_provider_id="review_catch", source=OuterLoopExperiment.Source.SIGNAL_REGRESSION
    )


def _park_a_converged_outer_loop() -> None:
    for _ in range(3):
        OuterLoopExperiment.objects.filter(pk=_proposed_experiment().pk).update(
            state=OuterLoopExperiment.State.REVERTED
        )
    run_tick(seams=_seams())


def _reask_an_undecidable_ratification() -> None:
    experiment = _proposed_experiment()
    question = DeferredQuestion.record(f"Ratify {experiment.pk}?", options_hash=f"outer_loop_ratify:{experiment.pk}")
    experiment.attach_ratification(question)
    DeferredQuestion.consume(question.pk, answer="not approved yet")
    try_admit(OuterLoopExperiment.objects.get(pk=experiment.pk))


def _kill_a_loop_tick() -> None:
    Loop.objects.update_or_create(
        name="dispatch", defaults={"script": "src/teatree/loops/dispatch/loop.py", "delay_seconds": 60, "enabled": True}
    )
    Loop.objects.update_or_create(
        name="inbox",
        defaults={
            "script": "src/teatree/loops/inbox/loop.py",
            "delay_seconds": 60,
            "enabled": True,
            "last_run_at": timezone.now() - dt.timedelta(seconds=120),
        },
    )
    context = SimpleNamespace(task_result=SimpleNamespace(id=uuid.uuid4()))
    with patch.object(timer_chains, "run_deadlined_tick", return_value={"timed_out": True, "returncode": None}):
        timer_chains.loop_timer.func(context, "inbox")


def _hold_an_oversize_work_group() -> None:
    members = range(1, WORK_GROUP_MAX_MEMBERS + 2)
    with _forge(_ListingHost(mrs=[_listed_mr(n, f"feat(billing): sweep step {n} ({_TICKET})") for n in members])):
        work_group_ready(mr_url=_listed_url(1))


def _survey_a_missing_review() -> None:
    with _channel(), _reads():
        MrTriageScanner(allowed_url_prefixes=_SCOPE, host=FakeCodeHost(user="alice", my_prs=[_opened(33)])).scan()


def _exhaust_a_review_nag() -> None:
    target = GuardTarget(channel_id="C0DEMOCHAN1", channel_name="review", token="xoxb-test")
    with (
        patch("teatree.core.gates.review_request_guard.resolve_guard_target", return_value=target),
        patch(
            "teatree.core.gates.review_request_guard.reconcile_out_of_band",
            return_value=ReconcileResult(ReconcileStatus.ABSENT),
        ),
        patch.object(cold_reader, "mapping_setting", return_value={"gitlab.example": ["owner"]}),
    ):
        _seed(days_old=200.0, nag_count=7)
        ReviewNagScanner(messaging=FakeSlack(), host=FakeHost()).scan()


# Board reconcile and the outer-loop tick sweep every row, so each runs before the drivers whose rows it would act on.
_OPERATIONAL_PRODUCERS: dict[Site, Callable[[], object]] = {
    ("src/teatree/loop/scanners/board_reconcile_issue_reopen.py", "_escalate_revival_cap_once"): (
        _revive_past_the_reopen_cap
    ),
    ("src/teatree/loops/outer_loop/tick.py", "_park_converged"): _park_a_converged_outer_loop,
    ("hooks/scripts/hook_router.py", "_capture_and_defer_question"): _deny_a_loop_driven_ask,
    ("src/teatree/agents/reactive_envelope_recorders.py", "_maybe_record_article_suggestions"): _record_a_news_batch,
    ("src/teatree/agents/review_envelope_recorder.py", "_latch_checks_contradiction"): _latch_a_spent_review_head,
    ("src/teatree/core/models/task_phase_disposition.py", "_record_hold_cap"): _hold_past_the_rework_cap,
    ("src/teatree/core/models/task_phase_disposition.py", "record_stuck_transition_question"): (
        _wedge_a_finished_phase
    ),
    ("src/teatree/core/models/task_repair.py", "_escalate_cap"): _spend_the_repair_cap,
    ("src/teatree/core/models/task_repair.py", "_escalate_reoffers"): _spend_the_reoffer_budget,
    ("src/teatree/core/models/task_repair.py", "_escalate_stall"): _stall_a_repair_loop,
    ("src/teatree/core/provision/failure_question.py", "record_provision_failure_question"): _fail_a_provision,
    ("src/teatree/core/tasks.py", "_record_attachment_hold_question"): _hold_an_unfetched_attachment,
    ("src/teatree/loop/ci_eval_heal_advance.py", "_escalate_via_deferred_question"): _halt_a_ci_eval_heal,
    ("src/teatree/loop/persistence_reviewer.py", "_escalate_unrecordable_review"): _refuse_an_unrecordable_review,
    ("src/teatree/loop/scanners/memory_skim.py", "scan"): _skim_promotable_memories,
    ("src/teatree/loop/stuck_ticket_redispatch.py", "_escalate_once"): _halt_a_stuck_ticket,
    ("src/teatree/loop/transient_requeue.py", "_escalate_once"): _halt_a_failed_task,
    ("src/teatree/loops/directive_loop/revert.py", "ask_revert"): lambda: ask_directive_revert(
        _directive_revert_pending()
    ),
    ("src/teatree/loops/outer_loop/keep.py", "ask_keep"): lambda: ask_keep(_keep_pending()),
    ("src/teatree/loops/outer_loop/ratify.py", "_undecidable_answer_question"): _reask_an_undecidable_ratification,
    ("src/teatree/loops/outer_loop/ratify.py", "ask_ratification"): lambda: ask_ratification(_proposed_experiment()),
    ("src/teatree/loops/outer_loop/revert.py", "ask_revert"): lambda: ask_experiment_revert(
        _experiment_revert_pending()
    ),
    ("src/teatree/loops/timer_chains.py", "_escalate_tick_timeout"): _kill_a_loop_tick,
    ("src/teatree/core/gates/review_request_batch_gate.py", "work_group_ready"): _hold_an_oversize_work_group,
    ("src/teatree/loop/scanners/mr_triage_scan.py", "scan"): _survey_a_missing_review,
    ("src/teatree/loop/scanners/review_nag.py", "_ask_owner_for_state"): _exhaust_a_review_nag,
}


@functools.cache
def _operational_sites() -> list[tuple[str, str, ast.Call]]:
    records = [site for site in _calls("record") if _kind(site[2]) is None]
    asks = [site for site in _calls("ask_mr_state") if not any(kw.arg == "owner" for kw in site[2].keywords)]
    return records + asks


def _site_of(frames: list[inspect.FrameInfo]) -> Site | None:
    root = REPO_ROOT.resolve()
    for frame in frames:
        path = Path(frame.filename).resolve()
        if (
            path.is_relative_to(root)
            and (site := (str(path.relative_to(root)), frame.function)) in _OPERATIONAL_PRODUCERS
        ):
            return site
    return None


@contextlib.contextmanager
def _recorded_by_site() -> Iterator[list[tuple[Site | None, DeferredQuestion]]]:
    recorded: list[tuple[Site | None, DeferredQuestion]] = []
    record = DeferredQuestion.record.__func__

    def spy(cls: type[DeferredQuestion], *args: object, **kwargs: object) -> DeferredQuestion:
        row = record(cls, *args, **kwargs)
        recorded.append((_site_of(inspect.stack(context=0)[1:]), row))
        return row

    with patch.object(DeferredQuestion, "record", classmethod(spy)):
        yield recorded


def _owner_dm_backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = "D_OWNER"
    backend.post_message.return_value = {"ok": True, "ts": "1700000000.000100"}
    backend.get_permalink.return_value = "https://acme.slack.com/archives/D_OWNER/p1700000000000100"
    return backend


class OperationalQuestionsNeverReachOwnerTests(TestCase):
    def test_every_operational_producer_is_classified(self) -> None:
        assert {(path, function) for path, function, _call in _operational_sites()} == set(_OPERATIONAL_PRODUCERS)

    def test_no_operational_producer_can_pass_a_decision_through_a_splat(self) -> None:
        splats = [
            (path, function)
            for path, function, call in _operational_sites()
            if any(isinstance(arg, ast.Starred) for arg in call.args)
            or any(keyword.arg is None for keyword in call.keywords)
        ]
        assert splats == []

    def test_each_producer_records_internal_rows_the_drain_never_posts_beside_an_owner_control(self) -> None:
        operational: list[DeferredQuestion] = []
        for site, drive in _OPERATIONAL_PRODUCERS.items():
            with self.subTest(path=site[0], function=site[1]):
                with _recorded_by_site() as recorded:
                    drive()
                rows = [row for where, row in recorded if where == site]
                assert rows, f"{site} recorded no question through its entry point"
                assert {row.audience for row in rows} == {DeferredQuestion.Audience.INTERNAL}
                operational.extend(rows)
        control = DeferredQuestion.record(
            "Which credential source should the eval run use?",
            decision=OwnerDecision.CREDENTIALS,
            checked=["t3 eval ci-account show lists zero eligible accounts"],
        )
        backend = _owner_dm_backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            drain_unmirrored_deferred_questions(user_id="U_OWNER", backend=backend)

        assert backend.post_message.call_count == 1
        assert control.question in backend.post_message.call_args.kwargs["text"]
        control.refresh_from_db()
        assert control.slack_ts
        assert not DeferredQuestion.objects.filter(pk__in=[row.pk for row in operational]).exclude(slack_ts="").exists()
