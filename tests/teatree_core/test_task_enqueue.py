"""Manual task enqueue preserves the landscape required by read-only planning."""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.agents.read_only_venue_brief import ReadOnlyFacts
from teatree.core.models import LandscapeArtifact, Task, Ticket
from teatree.core.task_enqueue import TaskEnqueueError, enqueue_phase_task, enqueue_phase_task_once
from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState

_SURVEY = {"open_prs": [{"url": "https://github.com/example/repo/pull/1"}], "worktrees": [], "warnings": []}


class TestManualPlanningEnqueue(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test")

    def test_missing_survey_is_recorded_once_and_delivered_to_the_brief(self) -> None:
        with patch("teatree.core.intake.landscape_persist.run_landscape", return_value=_SURVEY) as gather:
            first = enqueue_phase_task(ticket=self.ticket, phase="planning", reason="Plan the issue.")
            second = enqueue_phase_task(ticket=self.ticket, phase="planning", reason="Try planning again.")
            with (
                patch("teatree.agents.read_only_venue_brief._issue", return_value=("Issue body", "")),
                patch("teatree.agents.read_only_venue_brief._fetch_target"),
            ):
                facts = ReadOnlyFacts.gather(first, None)

        assert first.pk != second.pk
        assert LandscapeArtifact.objects.filter(ticket=self.ticket).count() == 1
        assert "https://github.com/example/repo/pull/1" in facts.survey
        gather.assert_called_once()
        assert gather.call_args.kwargs["overlay_name"] == "test"

    def test_unreadable_configured_forge_refuses_before_task_creation(self) -> None:
        missing = ForgeTokenResolution("github_token", "test", ForgeTokenState.UNREADABLE, detail="pass inaccessible")
        with (
            patch("teatree.core.intake.landscape_persist.resolve_named_overlay_token", return_value=missing),
            pytest.raises(TaskEnqueueError, match="pass inaccessible"),
        ):
            enqueue_phase_task(ticket=self.ticket, phase="planning", reason="Plan the issue.")

        assert not Task.objects.filter(ticket=self.ticket).exists()
        assert LandscapeArtifact.latest_for(self.ticket) is None

    def test_dashboard_enqueue_uses_the_same_survey_path(self) -> None:
        with patch("teatree.core.intake.landscape_persist.run_landscape", return_value=_SURVEY) as gather:
            enqueue_phase_task_once(ticket=self.ticket, phase="planning", reason="Plan the issue.")

        assert LandscapeArtifact.latest_for(self.ticket) is not None
        gather.assert_called_once()
