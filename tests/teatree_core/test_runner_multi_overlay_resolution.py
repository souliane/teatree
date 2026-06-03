"""Worktree-scoped runners must resolve the overlay from the worktree.

Regression for souliane/teatree#1814: with two overlays installed, the
queued FSM workers built ``WorktreeProvisionRunner(worktree)`` (and the
start/verify/service-launch siblings) with a bare ``get_overlay()`` that
cannot disambiguate, so every loop-driven worktree provision crashed with
``ImproperlyConfigured: Multiple overlays found``. Each runner now resolves
the overlay the worktree itself records.
"""

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayBase, ProvisionStep, RunCommands
from teatree.core.overlay_loader import get_overlay, get_overlay_for_ticket, get_overlay_for_worktree
from teatree.core.runners.service_launch import ServiceLauncher
from teatree.core.runners.worktree_provision import WorktreeProvisionRunner
from teatree.core.runners.worktree_start import WorktreeStartRunner
from teatree.core.runners.worktree_verify import WorktreeVerifyRunner


class _NamedOverlay(OverlayBase):
    def __init__(self, marker: str) -> None:
        super().__init__()
        self.marker = marker

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []

    def get_run_commands(self, worktree: Worktree) -> RunCommands:
        return {}


OVERLAY_A = "overlay-alpha"
OVERLAY_B = "overlay-beta"


class _MultiOverlayTest(TestCase):
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
        )


class TestMultiOverlayCrash(_MultiOverlayTest):
    def test_bare_get_overlay_is_ambiguous(self) -> None:
        with pytest.raises(ImproperlyConfigured, match="Multiple overlays found"):
            get_overlay()


class TestGetOverlayForTicket(_MultiOverlayTest):
    def test_resolves_ticket_overlay_field(self) -> None:
        ticket = Ticket.objects.create(overlay=OVERLAY_A, issue_url="https://example.com/t")
        assert get_overlay_for_ticket(ticket) is self.overlay_a

    def test_blank_overlay_is_still_ambiguous(self) -> None:
        ticket = Ticket.objects.create(overlay="", issue_url="https://example.com/blank-t")
        with pytest.raises(ImproperlyConfigured, match="Multiple overlays found"):
            get_overlay_for_ticket(ticket)


class TestGetOverlayForWorktree(_MultiOverlayTest):
    def test_resolves_worktree_overlay_field(self) -> None:
        worktree = self._worktree(overlay=OVERLAY_B)
        assert get_overlay_for_worktree(worktree) is self.overlay_b

    def test_falls_back_to_ticket_overlay_when_field_blank(self) -> None:
        ticket = Ticket.objects.create(overlay=OVERLAY_A, issue_url="https://example.com/blank")
        worktree = Worktree.objects.create(
            ticket=ticket,
            overlay="",
            repo_path="repo",
            branch="feat-x",
            extra={"worktree_path": "/tmp/wt"},
        )
        assert get_overlay_for_worktree(worktree) is self.overlay_a


class TestRunnersResolveWorktreeOverlay(_MultiOverlayTest):
    def test_provision_runner_resolves_worktree_overlay(self) -> None:
        worktree = self._worktree(overlay=OVERLAY_A)
        runner = WorktreeProvisionRunner(worktree)
        assert runner.overlay is self.overlay_a

    def test_provision_runner_resolves_other_overlay(self) -> None:
        worktree = self._worktree(overlay=OVERLAY_B)
        runner = WorktreeProvisionRunner(worktree)
        assert runner.overlay is self.overlay_b

    def test_start_runner_resolves_worktree_overlay(self) -> None:
        worktree = self._worktree(overlay=OVERLAY_B)
        runner = WorktreeStartRunner(worktree)
        assert runner.overlay is self.overlay_b

    def test_verify_runner_resolves_worktree_overlay(self) -> None:
        worktree = self._worktree(overlay=OVERLAY_A)
        runner = WorktreeVerifyRunner(worktree)
        assert runner.overlay is self.overlay_a

    def test_service_launcher_resolves_worktree_overlay(self) -> None:
        worktree = self._worktree(overlay=OVERLAY_B)
        launcher = ServiceLauncher(worktree, "backend")
        assert launcher.overlay is self.overlay_b

    def test_service_launcher_prepare_all_resolves_without_explicit_overlay(self) -> None:
        worktree = self._worktree(overlay=OVERLAY_A)
        with patch.object(ServiceLauncher, "_collect_steps", return_value=[]) as collect:
            ServiceLauncher.prepare_all(worktree, ["backend"])
        assert collect.call_args.args[0] is self.overlay_a
