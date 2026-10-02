"""``worktree status`` evaluates the worktree's OWN overlay's post-conditions.

Regression for teatree integration audit #24: the PR-27 post-condition probe
called ``evaluate_post_conditions(get_overlay(), worktree)`` — a bare
``get_overlay()`` that, with two overlays installed, cannot disambiguate and
raises ``ImproperlyConfigured: Multiple overlays found`` (or, worse, silently
resolves the wrong overlay). It now resolves via ``get_overlay_for_worktree``,
mirroring the #1975/#1814 runner convention, so each worktree is probed against
the overlay it records.
"""

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from teatree.core.management.commands import worktree as worktree_cmd
from teatree.core.management.commands.worktree import Command, WorktreeStatus
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayBase, OverlayRuntime, ProvisionStep, RunCommands

OVERLAY_A = "overlay-alpha"
OVERLAY_B = "overlay-beta"


class _NamedOverlayRuntime(OverlayRuntime):
    def run_commands(self, worktree: Worktree) -> RunCommands:
        return {}


class _NamedOverlay(OverlayBase):
    runtime = _NamedOverlayRuntime()

    def __init__(self, marker: str) -> None:
        super().__init__()
        self.marker = marker

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []


class _MultiOverlayStatusTest(TestCase):
    @pytest.fixture(autouse=True)
    def _register_both_overlays(self) -> Iterator[None]:
        self.overlay_a = _NamedOverlay(OVERLAY_A)
        self.overlay_b = _NamedOverlay(OVERLAY_B)
        registry = {OVERLAY_A: self.overlay_a, OVERLAY_B: self.overlay_b}
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=registry):
            yield

    def _worktree(self, *, overlay: str) -> Worktree:
        ticket = Ticket.objects.create(overlay=overlay, issue_url=f"https://example.com/{overlay}")
        return Worktree.objects.create(
            ticket=ticket,
            overlay=overlay,
            repo_path="repo",
            branch="feat-x",
            extra={"worktree_path": "/tmp/wt"},
            state=Worktree.State.PROVISIONED,
        )

    def _evaluate(self, worktree: Worktree) -> OverlayBase:
        """Run the probe and return the overlay it was evaluated against."""
        result: WorktreeStatus = {}
        with patch.object(worktree_cmd, "evaluate_post_conditions", return_value=([], 0)) as evaluate:
            Command()._evaluate_provision_post_conditions(worktree, result)
        return evaluate.call_args.args[0]


class TestStatusResolvesWorktreeOverlay(_MultiOverlayStatusTest):
    def test_probes_the_worktrees_own_overlay(self) -> None:
        worktree = self._worktree(overlay=OVERLAY_B)
        assert self._evaluate(worktree) is self.overlay_b

    def test_probes_the_other_overlay(self) -> None:
        worktree = self._worktree(overlay=OVERLAY_A)
        assert self._evaluate(worktree) is self.overlay_a

    def test_falls_back_to_ticket_overlay_when_field_blank(self) -> None:
        ticket = Ticket.objects.create(overlay=OVERLAY_A, issue_url="https://example.com/blank")
        worktree = Worktree.objects.create(
            ticket=ticket,
            overlay="",
            repo_path="repo",
            branch="feat-x",
            extra={"worktree_path": "/tmp/wt"},
            state=Worktree.State.PROVISIONED,
        )
        assert self._evaluate(worktree) is self.overlay_a

    def test_created_worktree_skips_post_conditions(self) -> None:
        ticket = Ticket.objects.create(overlay=OVERLAY_B, issue_url="https://example.com/created")
        worktree = Worktree.objects.create(
            ticket=ticket,
            overlay=OVERLAY_B,
            repo_path="repo",
            branch="feat-x",
            extra={"worktree_path": "/tmp/wt"},
            state=Worktree.State.CREATED,
        )
        result: WorktreeStatus = {}
        assert Command()._evaluate_provision_post_conditions(worktree, result) == 0


class TestBareGetOverlayWouldCrash(_MultiOverlayStatusTest):
    """The pre-fix shape: a bare ``get_overlay()`` cannot disambiguate two overlays."""

    def test_bare_get_overlay_is_ambiguous(self) -> None:
        with pytest.raises(ImproperlyConfigured, match="Multiple overlays found"):
            worktree_cmd.get_overlay()
