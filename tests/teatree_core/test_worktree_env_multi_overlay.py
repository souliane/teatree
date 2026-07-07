"""Env-cache rendering must resolve the overlay from the worktree, not bare get_overlay().

Regression for souliane/teatree#1975: ``render_env_cache`` (and the
``write_env_cache`` / ``detect_drift`` paths that funnel through it) called a
bare ``get_overlay()``. On a multi-overlay host that raises
``ImproperlyConfigured: Multiple overlays found``, so every queued
``execute_worktree_provision`` job crashed at env-cache rendering even though
the worktree row records its own overlay. The fix resolves the overlay the
worktree itself names; an explicit ``overlay=`` short-circuits re-resolution
for callers (the provision/start runners) that already hold the instance.
"""

import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayBase, OverlayProvisioning, ProvisionStep
from teatree.core.runners.worktree_provision import WorktreeProvisionRunner
from teatree.core.worktree.worktree_env import detect_drift, render_env_cache, write_env_cache
from teatree.core.worktree.worktree_tasks import execute_worktree_provision

OVERLAY_A = "overlay-alpha"
OVERLAY_B = "overlay-beta"


class _MarkedOverlay_Provisioning(OverlayProvisioning):
    def __init__(self, overlay: "_MarkedOverlay") -> None:
        self._overlay = overlay

    def env_extra(self, worktree: Worktree) -> dict[str, str]:
        return {"MARKER": self._overlay.marker}

    def declared_env_keys(self) -> set[str]:
        return {"MARKER"}


class _MarkedOverlay(OverlayBase):
    def __init__(self, marker: str) -> None:
        super().__init__()
        self.marker = marker
        self.provisioning = _MarkedOverlay_Provisioning(self)

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []




class _MultiOverlayEnvTest(TestCase):
    @pytest.fixture(autouse=True)
    def _register_both_overlays(self) -> Iterator[None]:
        self.overlay_a = _MarkedOverlay(OVERLAY_A)
        self.overlay_b = _MarkedOverlay(OVERLAY_B)
        registry = {OVERLAY_A: self.overlay_a, OVERLAY_B: self.overlay_b}
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=registry):
            yield

    def _worktree(self, tmp: str, *, overlay: str, ticket_overlay: str | None = None) -> Worktree:
        ticket_dir = Path(tmp) / "ticket"
        ticket_dir.mkdir(exist_ok=True)
        wt_path = ticket_dir / "backend"
        wt_path.mkdir(exist_ok=True)
        ticket = Ticket.objects.create(
            overlay=ticket_overlay if ticket_overlay is not None else overlay,
            issue_url="https://example.com/1975",
        )
        return Worktree.objects.create(
            ticket=ticket,
            overlay=overlay,
            repo_path="backend",
            branch="feat-x",
            db_name="wt_1975",
            state=Worktree.State.PROVISIONED,
            extra={"worktree_path": str(wt_path)},
        )


class TestRenderEnvCacheResolvesWorktreeOverlay(_MultiOverlayEnvTest):
    def test_render_resolves_worktree_overlay_on_multi_overlay_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt = self._worktree(tmp, overlay=OVERLAY_A)
            spec = render_env_cache(wt)
        assert spec is not None
        assert f"MARKER={OVERLAY_A}" in spec.content

    def test_render_picks_the_other_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt = self._worktree(tmp, overlay=OVERLAY_B)
            spec = render_env_cache(wt)
        assert spec is not None
        assert f"MARKER={OVERLAY_B}" in spec.content

    def test_render_falls_back_to_ticket_overlay_when_field_blank(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt = self._worktree(tmp, overlay="", ticket_overlay=OVERLAY_A)
            spec = render_env_cache(wt)
        assert spec is not None
        assert f"MARKER={OVERLAY_A}" in spec.content

    def test_explicit_overlay_argument_short_circuits_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt = self._worktree(tmp, overlay=OVERLAY_A)
            spec = render_env_cache(wt, overlay=self.overlay_b)
        assert spec is not None
        assert f"MARKER={OVERLAY_B}" in spec.content


class TestWriteAndDriftResolveWorktreeOverlay(_MultiOverlayEnvTest):
    def test_write_env_cache_succeeds_on_multi_overlay_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt = self._worktree(tmp, overlay=OVERLAY_B)
            spec = write_env_cache(wt)
            assert spec is not None
            assert f"MARKER={OVERLAY_B}" in spec.path.read_text(encoding="utf-8")

    def test_detect_drift_succeeds_on_multi_overlay_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt = self._worktree(tmp, overlay=OVERLAY_A)
            write_env_cache(wt)
            drifted, _ = detect_drift(wt)
        assert drifted is False


class TestProvisionRunnerEnvCacheOnMultiOverlayHost(_MultiOverlayEnvTest):
    def test_runner_writes_env_cache_without_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt = self._worktree(tmp, overlay=OVERLAY_B)
            result = WorktreeProvisionRunner(wt).run()
            assert result.ok, result.detail
            spec_path = Path(wt.worktree_path).parent / ".t3-cache" / ".t3-env.cache"
            assert spec_path.is_file()
            assert f"MARKER={OVERLAY_B}" in spec_path.read_text(encoding="utf-8")

    def test_queued_provision_job_completes_on_multi_overlay_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt = self._worktree(tmp, overlay=OVERLAY_A)
            result = execute_worktree_provision.call(wt.pk)
        assert result == {"worktree_id": wt.pk, "ok": True, "detail": result["detail"]}
        assert result["ok"] is True


class TestProvisionPoisonPillGuard(TestCase):
    """An unresolvable overlay must fail permanently, not raise every re-fire (#1975/#1969)."""

    @pytest.fixture(autouse=True)
    def _register_one_overlay(self) -> Iterator[None]:
        registry = {OVERLAY_A: _MarkedOverlay(OVERLAY_A)}
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=registry):
            yield

    def test_unknown_overlay_worktree_fails_permanently(self) -> None:
        ticket = Ticket.objects.create(overlay="gone-overlay", issue_url="https://example.com/poison")
        wt = Worktree.objects.create(
            ticket=ticket,
            overlay="gone-overlay",
            repo_path="backend",
            branch="feat-x",
            state=Worktree.State.PROVISIONED,
            extra={"worktree_path": "/tmp/wt"},
        )
        result = execute_worktree_provision.call(wt.pk)
        assert result["ok"] is False
        assert "gone-overlay" in result["detail"]
