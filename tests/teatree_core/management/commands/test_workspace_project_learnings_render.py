"""``workspace ticket`` intake surfaces project learnings collapsed (#2892).

The project-scoped sibling of ``test_workspace_ticket_context_render.py``
(#627): the intake summary also renders any durable per-repo learnings
recorded for the ticket's repo, so a fresh session sees prior project-scoped
lessons before doing work (the "skill-flow consumption" deliverable of
#2892) without an explicit lookup. Same rendering shape as
``render_ticket_context`` — a pure function is unit-tested directly; the
wiring is verified through the real ``workspace ticket`` command.
"""

import os
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings

import teatree.core.management.commands.workspace as workspace_mod
import teatree.utils.run as utils_run_mod
from teatree.core.models.project_learning import ProjectLearning
from teatree.core.models.ticket_display import render_project_learnings
from tests.teatree_core.management_commands._overlays import FULL_OVERLAY, SETTINGS, _patch_overlays

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


class RenderProjectLearningsTest(TestCase):
    def test_empty_content_renders_nothing(self) -> None:
        assert render_project_learnings("") == ""
        assert render_project_learnings("   \n  ") == ""

    def test_present_content_renders_collapsed_block(self) -> None:
        text = render_project_learnings("[2026-05-18 09:00] de-CH locale only, never it-CH")
        assert text.startswith("\n\n")
        assert "<details>" in text
        assert "<summary>Project learnings (durable knowledge store)</summary>" in text
        assert "de-CH locale only" in text
        assert "</details>" in text
        assert "truncated" not in text

    def test_long_content_is_truncated_with_pointer(self) -> None:
        body = "\n".join(f"[2026-05-18 09:{i:02d}] lesson {i}" for i in range(60))
        text = render_project_learnings(body, max_lines=40)
        assert "<details>" in text
        assert "lesson 0" in text
        assert "lesson 59" not in text
        assert "20 more line(s) truncated" in text
        assert "learnings show" in text


class WorkspaceTicketProjectLearningsWiringTest(TestCase):
    def setUp(self) -> None:
        super().setUp()
        mock_result = MagicMock(returncode=0, stdout="dev", stderr="")
        self.enterContext(patch.object(utils_run_mod.subprocess, "run", return_value=mock_result))
        workspace = Path(os.environ["HOME"]) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        for repo in ("backend", "frontend"):
            (workspace / repo / ".git").mkdir(parents=True, exist_ok=True)

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_intake_renders_recorded_project_learnings(self) -> None:
        ProjectLearning.objects.create(
            repo_slug="acme-eng/widgets",
            content="\n\n[2026-05-18 09:00] de-CH locale only, never it-CH",
        )
        out = StringIO()
        with patch.object(workspace_mod.Command, "print_result", new=False, create=True):
            cmd = workspace_mod.Command(stdout=out)
            call_command(cmd, "ticket", "https://github.com/acme-eng/widgets/issues/601")
        text = out.getvalue()
        assert "<details>" in text
        assert "Project learnings (durable knowledge store)" in text
        assert "de-CH locale only" in text

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_intake_omits_section_when_no_learnings_recorded(self) -> None:
        out = StringIO()
        with patch.object(workspace_mod.Command, "print_result", new=False, create=True):
            cmd = workspace_mod.Command(stdout=out)
            call_command(cmd, "ticket", "https://github.com/acme-eng/widgets/issues/602")
        text = out.getvalue()
        assert "Project learnings" not in text

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_intake_never_confuses_two_repos_sharing_an_issue_number(self) -> None:
        ProjectLearning.objects.create(repo_slug="acme-eng/bugs", content="bugs-repo lesson")
        ProjectLearning.objects.create(repo_slug="acme-product/repo", content="product-repo lesson")

        out = StringIO()
        with patch.object(workspace_mod.Command, "print_result", new=False, create=True):
            cmd = workspace_mod.Command(stdout=out)
            call_command(cmd, "ticket", "https://github.com/acme-eng/bugs/issues/2242")
        text = out.getvalue()
        assert "bugs-repo lesson" in text
        assert "product-repo lesson" not in text
