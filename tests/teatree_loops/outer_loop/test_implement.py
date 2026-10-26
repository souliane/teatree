"""IMPLEMENT — synthetic ticket planned first, then the maker pipeline (T4-PR-3)."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from teatree.core.admission.pr_start_cap import OPEN_PR_START_CAP
from teatree.core.models import (
    DeferredQuestion,
    FactoryScoreSnapshot,
    OuterLoopExperiment,
    ProposalSpec,
    PullRequest,
    Task,
    Ticket,
)
from teatree.loops.outer_loop.implement import schedule_experiment_fix


class TestScheduleExperimentFix(TestCase):
    def test_open_pr_cap_holds_new_experiment_ticket(self) -> None:
        experiment = self._admitted()
        for index in range(OPEN_PR_START_CAP):
            PullRequest.objects.record_opened(
                ticket=Ticket.objects.create(),
                url=f"https://github.com/example-org/example-repo/pull/{index + 900}",
            )
        assert schedule_experiment_fix(experiment) is None
        experiment.refresh_from_db()
        assert experiment.state == OuterLoopExperiment.State.ADMITTED
        assert experiment.ticket_id is None

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
        overlays = {
            "umbrella-owner": SimpleNamespace(get_workspace_repos=lambda: ["souliane/teatree"]),
            "factory-owner": SimpleNamespace(get_workspace_repos=lambda: ["vendor/factory"]),
        }
        for slug, expected in (("vendor/factory", "factory-owner"), ("unclaimed/repo", "")):
            with (
                self.subTest(slug=slug),
                patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays),
                patch("teatree.utils.git.remote_slug", return_value=slug),
            ):
                task = schedule_experiment_fix(self._admitted())
                assert task is not None
                assert task.ticket.overlay == expected

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
