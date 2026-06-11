"""Ticket model tests (souliane/teatree#443 split of test_models.py).

Number derivation, locked ``extra`` RMW, FSM transitions, and the
shippable-diff gate.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import E2eMandatoryRun, PlanArtifact, Session, Task, TaskAttempt, Ticket, Worktree
from tests.teatree_core.models._shared import (
    _advance_started_to_planned,
    _advance_ticket_to_tested,
    _attach_shippable_worktree,
    _complete_phase_task,
    _init_repo_with_branch,
)


class TestTicketNumber(TestCase):
    """``Ticket.ticket_number`` derives a stable identifier from ``issue_url``.

    The fallback to ``str(self.pk)`` covers issue URLs that do not end in a
    valid issue number — empty, non-numeric suffix, or the placeholder ``/0``
    that GitHub and GitLab never assign to a real issue (issue numbers start at 1).
    """

    def test_returns_trailing_number_when_url_is_well_formed(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/example/repo/issues/123")
        assert ticket.ticket_number == "123"

    def test_falls_back_to_pk_when_url_is_empty(self) -> None:
        ticket = Ticket.objects.create()
        assert ticket.ticket_number == str(ticket.pk)

    def test_falls_back_to_pk_when_url_has_no_trailing_number(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/no-number")
        assert ticket.ticket_number == str(ticket.pk)

    def test_falls_back_to_pk_when_url_ends_in_zero(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/example/repo/issues/0")
        assert ticket.ticket_number == str(ticket.pk)


class TestTicketMergeExtra(TestCase):
    """#800 N3: the canonical locked ``extra`` RMW primitive.

    Behaviour contract (the concurrency proof is the file-backed-SQLite
    harness in ``tests/test_ticket_extra_merge_serialization.py``); here
    we pin the API: set/pop semantics and that a re-read from the DB
    between two calls is merged, not clobbered (the locked primitive
    re-reads the row, so it never overwrites a key it did not touch).
    """

    def test_set_keys_persists_and_merges_existing(self) -> None:
        ticket = Ticket.objects.create(extra={"keep": 1})
        ticket.merge_extra(set_keys={"pr_urls": ["u"]})
        ticket.refresh_from_db()
        assert ticket.extra == {"keep": 1, "pr_urls": ["u"]}

    def test_pop_keys_removes_only_named(self) -> None:
        ticket = Ticket.objects.create(extra={"a": 1, "ship_invoking_branch": "b"})
        ticket.merge_extra(pop_keys=["ship_invoking_branch"])
        ticket.refresh_from_db()
        assert ticket.extra == {"a": 1}

    def test_does_not_clobber_a_concurrent_writers_key(self) -> None:
        # Two handles to the same row (the lost-update setup): a stale
        # in-memory instance and a fresh write by "another worker".
        ticket = Ticket.objects.create(extra={})
        stale = Ticket.objects.get(pk=ticket.pk)
        Ticket.objects.filter(pk=ticket.pk).update(extra={"visual_qa": {"x": 1}})
        # The stale handle's merge must NOT wipe visual_qa — the locked
        # re-read inside merge_extra sees the other worker's commit.
        stale.merge_extra(set_keys={"pr_urls": ["u"]})
        ticket.refresh_from_db()
        assert ticket.extra == {"visual_qa": {"x": 1}, "pr_urls": ["u"]}

    def test_set_and_pop_in_one_call(self) -> None:
        ticket = Ticket.objects.create(extra={"pr_title_override": "t"})
        ticket.merge_extra(set_keys={"pr_urls": ["u"]}, pop_keys=["pr_title_override"])
        ticket.refresh_from_db()
        assert ticket.extra == {"pr_urls": ["u"]}

    def test_noop_call_persists_current_state(self) -> None:
        ticket = Ticket.objects.create(extra={"a": 1})
        ticket.merge_extra()
        ticket.refresh_from_db()
        assert ticket.extra == {"a": 1}

    def test_also_set_writes_sibling_fields_in_the_same_locked_update(self) -> None:
        # The tracker-sync paths co-write extra + state/repos in one
        # save; also_set keeps that atomic through the locked primitive.
        ticket = Ticket.objects.create(extra={"keep": 1}, repos=["a"])
        ticket.merge_extra(
            set_keys={"prs": {}},
            also_set={"repos": ["a", "b"], "variant": "x"},
        )
        ticket.refresh_from_db()
        assert ticket.extra == {"keep": 1, "prs": {}}
        assert ticket.repos == ["a", "b"]
        assert ticket.variant == "x"
        # The in-memory instance is updated too (no stale read after).
        assert ticket.variant == "x"

    def test_also_set_does_not_clobber_concurrent_extra_writer(self) -> None:
        ticket = Ticket.objects.create(extra={})
        stale = Ticket.objects.get(pk=ticket.pk)
        Ticket.objects.filter(pk=ticket.pk).update(extra={"visual_qa": {"x": 1}})
        stale.merge_extra(set_keys={"prs": {}}, also_set={"variant": "v"})
        ticket.refresh_from_db()
        assert ticket.extra == {"visual_qa": {"x": 1}, "prs": {}}
        assert ticket.variant == "v"


class TestTicketTransitions(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def test_persist_delivery_state(self) -> None:
        ticket = Ticket.objects.create()
        _attach_shippable_worktree(ticket, self._tmp_path)

        _advance_ticket_to_tested(ticket)

        # #1284: ``_advance_ticket_to_tested`` fires ``test()`` directly on
        # the FSM so the testing phase visit is not recorded. Record it
        # symmetrically with how the loop would have done it on the testing
        # task's completion — the shipping gate now enforces the visited
        # phases the ``pr create`` path always required.
        testing_session = Session.objects.create(ticket=ticket, agent_id="testing")
        testing_session.visit_phase("testing", agent_id="testing")

        # test() auto-scheduled a reviewing task — complete it to unlock review()
        _complete_phase_task(ticket, "reviewing")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED

        # review() auto-scheduled a shipping task — complete it to unlock ship()
        _complete_phase_task(ticket, "shipping")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED

        ticket.request_review()
        ticket.save()
        ticket.mark_merged()
        ticket.save()
        ticket.retrospect()
        ticket.save()
        ticket.mark_delivered()
        ticket.save()

        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.DELIVERED
        assert ticket.issue_url == "https://example.com/issues/123"
        assert ticket.variant == "acme"
        assert ticket.repos == ["backend", "frontend"]
        assert ticket.extra["tests_passed"] is True
        assert str(ticket) == "https://example.com/issues/123"

    def test_auto_schedules_review_task(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()
        ticket.start()
        ticket.save()
        _advance_started_to_planned(ticket)
        ticket.code()
        ticket.save()
        ticket.test()
        ticket.save()

        # test() auto-schedules a reviewing task; reviewing is loop-dispatched
        # ((author, reviewing) → t3:reviewer) so it runs in-session.
        task = ticket.tasks.get(phase="reviewing")
        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE
        assert task.session.agent_id == "review"
        assert ticket.state == Ticket.State.TESTED

    def test_review_blocked_without_completed_review_task(self) -> None:
        from django_fsm import TransitionNotAllowed  # noqa: PLC0415

        ticket = Ticket.objects.create()
        _advance_ticket_to_tested(ticket)

        with pytest.raises(TransitionNotAllowed):
            ticket.review()

    def test_reviewing_task_completion_advances_to_reviewed(self) -> None:
        ticket = Ticket.objects.create()
        _attach_shippable_worktree(ticket, self._tmp_path)
        _advance_ticket_to_tested(ticket)

        _complete_phase_task(ticket, "reviewing")
        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.REVIEWED
        # review() also auto-scheduled a shipping task
        assert ticket.tasks.filter(phase="shipping", status=Task.Status.PENDING).exists()

    def test_rework_cancels_pending_tasks(self) -> None:
        ticket = Ticket.objects.create()
        _advance_ticket_to_tested(ticket)

        # There's a pending reviewing task from test()
        assert ticket.tasks.filter(phase="reviewing", status=Task.Status.PENDING).exists()

        ticket.rework()
        ticket.save()

        # Pending tasks should now be failed
        assert not ticket.tasks.filter(status=Task.Status.PENDING).exists()
        assert ticket.tasks.filter(status=Task.Status.FAILED).exists()

    def test_needs_user_input_creates_interactive_followup(self) -> None:
        ticket = Ticket.objects.create()
        _advance_ticket_to_tested(ticket)

        task = ticket.tasks.get(phase="reviewing")
        task.claim(claimed_by="worker")

        # Simulate agent output with needs_user_input
        TaskAttempt.objects.create(
            task=task,
            execution_target=task.execution_target,
            exit_code=0,
            result={"needs_user_input": True, "user_input_reason": "Need design decision"},
        )
        task.complete()

        # Should NOT advance ticket (needs_user_input blocks it)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.TESTED

        # Should have created a new interactive task
        interactive = ticket.tasks.filter(
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.PENDING,
        )
        assert interactive.count() == 1
        assert interactive.first().execution_reason == "Need design decision"

    def test_rework_returns_to_started_and_clears_testing_fact(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()
        ticket.start()
        ticket.save()
        _advance_started_to_planned(ticket)
        ticket.code()
        ticket.save()
        ticket.test(passed=True)
        ticket.save()

        ticket.rework()
        ticket.save()
        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.STARTED
        assert "tests_passed" not in ticket.extra

    def test_ignore_hides_ticket_from_in_flight(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()
        ticket.start()
        ticket.save()

        assert ticket in Ticket.objects.in_flight()

        ticket.ignore()
        ticket.save()
        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.IGNORED
        assert ticket.extra["ignored_from"] == "started"
        assert ticket not in Ticket.objects.in_flight()

    def test_unignore_restores_previous_state(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()
        ticket.start()
        ticket.save()
        _advance_started_to_planned(ticket)
        ticket.code()
        ticket.save()

        ticket.ignore()
        ticket.save()
        assert ticket.state == Ticket.State.IGNORED

        ticket.unignore()
        ticket.save()
        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.CODED
        assert "ignored_from" not in ticket.extra

    def test_rejects_invalid_transition(self) -> None:
        ticket = Ticket.objects.create()

        from django_fsm import TransitionNotAllowed  # noqa: PLC0415

        with pytest.raises(TransitionNotAllowed):
            ticket.review()


class TestHasShippableDiff(TestCase):
    """``Ticket.has_shippable_diff`` and the auto-shipping gate (issue #473)."""

    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _make_ticket_with_worktree(self, *, commits_ahead: int) -> Ticket:
        ticket = Ticket.objects.create()
        repo_dir = self._tmp_path / f"repo-{commits_ahead}"
        branch = "feature"
        _init_repo_with_branch(repo_dir, branch=branch, commits_ahead=commits_ahead)
        Worktree.objects.create(ticket=ticket, repo_path=str(repo_dir), branch=branch)
        return ticket

    def test_returns_false_when_no_worktrees(self) -> None:
        ticket = Ticket.objects.create()

        assert ticket.has_shippable_diff() is False

    def test_returns_false_when_branch_has_no_commits_ahead(self) -> None:
        ticket = self._make_ticket_with_worktree(commits_ahead=0)

        assert ticket.has_shippable_diff() is False

    def test_returns_true_when_branch_has_commits_ahead(self) -> None:
        ticket = self._make_ticket_with_worktree(commits_ahead=2)

        assert ticket.has_shippable_diff() is True

    def test_review_skips_shipping_task_when_no_diff(self) -> None:
        ticket = self._make_ticket_with_worktree(commits_ahead=0)
        _advance_ticket_to_tested(ticket)

        _complete_phase_task(ticket, "reviewing")
        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.REVIEWED
        assert not ticket.tasks.filter(phase="shipping").exists()
        assert "no shippable diff" in ticket.extra.get("shipping_skipped", "")

    def test_review_schedules_shipping_when_branch_has_commits(self) -> None:
        ticket = self._make_ticket_with_worktree(commits_ahead=1)
        _advance_ticket_to_tested(ticket)

        _complete_phase_task(ticket, "reviewing")
        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.REVIEWED
        assert ticket.tasks.filter(phase="shipping", status=Task.Status.PENDING).exists()
        assert "shipping_skipped" not in ticket.extra

    def test_returns_false_when_worktree_missing_branch(self) -> None:
        ticket = Ticket.objects.create()
        Worktree.objects.create(ticket=ticket, repo_path=str(self._tmp_path), branch="")

        assert ticket.has_shippable_diff() is False

    def test_returns_false_when_repo_path_is_not_a_git_directory(self) -> None:
        ticket = Ticket.objects.create()
        not_a_repo = self._tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        Worktree.objects.create(ticket=ticket, repo_path=str(not_a_repo), branch="feature")

        assert ticket.has_shippable_diff() is False


class TestHasDispatchableOverlay(TestCase):
    """``Ticket.has_dispatchable_overlay`` — the single poison-pill predicate (#1959)."""

    def test_blank_overlay_is_dispatchable(self) -> None:
        ticket = Ticket.objects.create(overlay="")

        assert ticket.has_dispatchable_overlay() is True

    def test_resolvable_overlay_is_dispatchable(self) -> None:
        ticket = Ticket.objects.create(overlay="known-overlay")

        with patch("teatree.core.overlay_loader.resolve_overlay_name", return_value="known-overlay"):
            assert ticket.has_dispatchable_overlay() is True

    def test_unresolvable_overlay_is_poison(self) -> None:
        ticket = Ticket.objects.create(overlay="ghost-overlay")

        with patch("teatree.core.overlay_loader.resolve_overlay_name", return_value=None):
            assert ticket.has_dispatchable_overlay() is False


class TestTicketArtifacts(TestCase):
    """``Ticket.artifacts`` — read-only per-ticket "find our eggs" aggregation (#273).

    Collects, over EXISTING related rows (no new storage model): the ticket's
    worktrees (on-disk path, ports, db_name, state), PlanArtifact rows, each
    Task's ``result_artifact_path``, and E2eMandatoryRun evidence (spec + posted
    video/comment URL). The port resolver is injected so the model method stays
    pure — no live docker query in the model.
    """

    def _ports(self, _worktree: Worktree) -> dict[str, int]:
        return {"backend": 18000, "frontend": 18080}

    def test_empty_ticket_yields_empty_artifacts(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/example/repo/issues/273")

        artifacts = ticket.artifacts()

        assert artifacts.ticket_id == ticket.pk
        assert artifacts.worktrees == ()
        assert artifacts.plan_artifacts == ()
        assert artifacts.result_artifact_paths == ()
        assert artifacts.e2e_runs == ()

    def test_collects_worktree_path_ports_db_name_and_state(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/example/repo/issues/273")
        Worktree.objects.create(
            ticket=ticket,
            repo_path="example/repo",
            branch="ac/273",
            db_name="wt_273",
            state=Worktree.State.READY,
            extra={"worktree_path": "/ws/273/example-repo"},
        )

        artifacts = ticket.artifacts(port_resolver=self._ports)

        assert len(artifacts.worktrees) == 1
        wt = artifacts.worktrees[0]
        assert wt.worktree_path == "/ws/273/example-repo"
        assert wt.db_name == "wt_273"
        assert wt.state == Worktree.State.READY
        assert wt.repo_path == "example/repo"
        assert wt.branch == "ac/273"
        assert wt.ports == {"backend": 18000, "frontend": 18080}

    def test_ports_default_to_empty_without_a_resolver(self) -> None:
        ticket = Ticket.objects.create()
        Worktree.objects.create(
            ticket=ticket,
            repo_path="example/repo",
            branch="ac/273",
            extra={"worktree_path": "/ws/273/example-repo"},
        )

        artifacts = ticket.artifacts()

        assert artifacts.worktrees[0].ports == {}

    def test_collects_plan_artifacts(self) -> None:
        ticket = Ticket.objects.create()
        PlanArtifact.record(ticket=ticket, plan_text="the plan", recorded_by="planner")

        artifacts = ticket.artifacts()

        assert len(artifacts.plan_artifacts) == 1
        plan = artifacts.plan_artifacts[0]
        assert plan.plan_text == "the plan"
        assert plan.recorded_by == "planner"

    def test_collects_task_result_artifact_paths_skipping_blanks(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        Task.objects.create(ticket=ticket, session=session, phase="coding", result_artifact_path="/runs/a.jsonl")
        Task.objects.create(ticket=ticket, session=session, phase="testing", result_artifact_path="/runs/b.jsonl")
        # A task with no recorded artifact path must not surface as a blank "egg".
        Task.objects.create(ticket=ticket, session=session, phase="review", result_artifact_path="")

        artifacts = ticket.artifacts()

        assert set(artifacts.result_artifact_paths) == {"/runs/a.jsonl", "/runs/b.jsonl"}

    def test_collects_e2e_runs_with_spec_and_posted_video_url(self) -> None:
        ticket = Ticket.objects.create()
        E2eMandatoryRun.record(
            ticket=ticket,
            head_sha="a" * 40,
            spec="e2e/login.spec.ts",
            result=E2eMandatoryRun.Result.GREEN,
            posted_url="https://github.com/example/repo/issues/273#comment-1",
        )

        artifacts = ticket.artifacts()

        assert len(artifacts.e2e_runs) == 1
        run = artifacts.e2e_runs[0]
        assert run.spec == "e2e/login.spec.ts"
        assert run.result == E2eMandatoryRun.Result.GREEN
        assert run.posted_url == "https://github.com/example/repo/issues/273#comment-1"
        assert run.head_sha == "a" * 40

    def test_aggregates_all_sources_together(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/example/repo/issues/273")
        Worktree.objects.create(
            ticket=ticket,
            repo_path="example/repo",
            branch="ac/273",
            db_name="wt_273",
            extra={"worktree_path": "/ws/273/example-repo"},
        )
        PlanArtifact.record(ticket=ticket, plan_text="plan", recorded_by="planner")
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        Task.objects.create(ticket=ticket, session=session, phase="coding", result_artifact_path="/runs/a.jsonl")
        E2eMandatoryRun.record(
            ticket=ticket,
            head_sha="b" * 40,
            spec="e2e/flow.spec.ts",
            result=E2eMandatoryRun.Result.GREEN,
            posted_url="https://example.com/c1",
        )

        artifacts = ticket.artifacts(port_resolver=self._ports)

        assert len(artifacts.worktrees) == 1
        assert len(artifacts.plan_artifacts) == 1
        assert artifacts.result_artifact_paths == ("/runs/a.jsonl",)
        assert len(artifacts.e2e_runs) == 1

    def test_artifacts_are_immutable(self) -> None:
        ticket = Ticket.objects.create()

        artifacts = ticket.artifacts()

        with pytest.raises(AttributeError):
            artifacts.ticket_id = 99  # frozen dataclass rejects mutation
