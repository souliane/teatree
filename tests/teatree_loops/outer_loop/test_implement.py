"""IMPLEMENT — synthetic ticket planned first, then the maker pipeline (T4-PR-3)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from teatree.core.models import (
    DeferredQuestion,
    FactoryScoreSnapshot,
    OuterLoopExperiment,
    ProposalSpec,
    PullRequest,
    Task,
    Ticket,
)
from teatree.core.overlay_loader import get_overlay_for_ticket
from teatree.loops.outer_loop.implement import schedule_experiment_fix
from tests.loop_ticket_git import installed_checkout_owner, installed_package_without_git


class TestScheduleExperimentFix(TestCase):
    def test_twenty_live_pull_requests_allow_new_experiment_ticket(self) -> None:
        experiment = self._admitted()
        for index in range(20):
            PullRequest.objects.record_opened(
                ticket=Ticket.objects.create(),
                url=f"https://github.com/example-org/example-repo/pull/{index + 900}",
            )
        task = schedule_experiment_fix(experiment)
        experiment.refresh_from_db()
        assert task is not None
        assert experiment.state == OuterLoopExperiment.State.IMPLEMENTING
        assert experiment.ticket_id == task.ticket_id

    def _admitted(self, *, overlay: str = "") -> OuterLoopExperiment:
        exp = _make_experiment(
            overlay=overlay,
            hypothesis="Improve the review gate.",
            target_provider_id="review_catch",
            source=OuterLoopExperiment.Source.SIGNAL_REGRESSION,
        )
        question = DeferredQuestion.record("Ratify?")
        DeferredQuestion.consume(question.pk, answer="approve")
        exp.attach_ratification(DeferredQuestion.objects.get(pk=question.pk))
        exp.admit()
        return exp

    def test_explicit_overlay_is_stored_despite_umbrella_owner(self) -> None:
        overlays = {"umbrella-owner": SimpleNamespace(get_workspace_repos=lambda: ["souliane/teatree"])}
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays):
            task = schedule_experiment_fix(self._admitted(overlay="another-overlay"))
        assert task is not None
        assert task.ticket.overlay == "another-overlay"

    def test_blank_overlay_uses_only_the_unique_installed_checkout_owner(self) -> None:
        for claimed, count in ((True, 1), (False, 1), (True, 2)):
            with (
                self.subTest(claimed=claimed, count=count),
                installed_checkout_owner(claimed=claimed, registered_overlays=count) as expected,
            ):
                task = schedule_experiment_fix(self._admitted())
                assert task is not None
                assert task.ticket.overlay == expected

    def test_package_without_git_uses_only_the_sole_registered_overlay(self) -> None:
        for count in (0, 1, 2):
            with self.subTest(count=count), installed_package_without_git(registered_overlays=count) as expected:
                task = schedule_experiment_fix(self._admitted())
                assert task is not None
                assert task.ticket.overlay == expected
                if count != 1:
                    with pytest.raises(ImproperlyConfigured, match="has no overlay"):
                        get_overlay_for_ticket(task.ticket)

    def test_existing_ticket_does_not_resolve_creation_overlay(self) -> None:
        experiment = self._admitted(overlay="first-scope")
        schedule_experiment_fix(experiment)
        experiment.state = OuterLoopExperiment.State.ADMITTED
        experiment.save(update_fields=["state"])
        with patch.object(Ticket, "creation_overlay") as resolver:
            assert schedule_experiment_fix(experiment) is None
        resolver.assert_not_called()

    def test_creates_a_synthetic_ticket_and_transitions(self) -> None:
        exp = self._admitted()
        schedule_experiment_fix(exp)
        reloaded = OuterLoopExperiment.objects.get(pk=exp.pk)
        assert reloaded.state == OuterLoopExperiment.State.IMPLEMENTING
        assert reloaded.ticket is not None
        ticket = Ticket.objects.get(pk=reloaded.ticket_id)
        assert f"outer-loop-experiment={exp.pk}" in ticket.issue_url
        assert ticket.extra["outer_loop_experiment_id"] == exp.pk

    def test_the_fresh_ticket_is_planned_first_carrying_the_hypothesis(self) -> None:
        exp = self._admitted()
        task = schedule_experiment_fix(exp)
        assert task is not None
        assert task.phase == "planning"
        assert "Improve the review gate." in task.execution_reason
        assert not Task.objects.filter(ticket=task.ticket, phase="coding").exists()

    def test_is_idempotent_on_the_synthetic_issue_url(self) -> None:
        exp = self._admitted(overlay="first-scope")
        schedule_experiment_fix(exp)
        before = Ticket.objects.count()
        # Re-driving the same experiment must not create a second ticket (the
        # unique synthetic issue URL dedups) — begin_implementation guards re-entry.
        exp2 = OuterLoopExperiment.objects.get(pk=exp.pk)
        exp2.overlay = "second-scope"
        exp2.save(update_fields=["overlay"])
        exp2.state = OuterLoopExperiment.State.ADMITTED
        exp2.save(update_fields=["state"])
        schedule_experiment_fix(exp2)
        assert Ticket.objects.count() == before
        exp2.refresh_from_db()
        assert exp2.ticket.overlay == "first-scope"


def _make_experiment(
    *,
    overlay: str = "",
    baseline_snapshot: FactoryScoreSnapshot | None = None,
    **spec_kw: object,
) -> OuterLoopExperiment:
    """Build an experiment via the ProposalSpec factory (test convenience)."""
    return OuterLoopExperiment.objects.propose(
        ProposalSpec(**spec_kw), overlay=overlay, baseline_snapshot=baseline_snapshot
    )
