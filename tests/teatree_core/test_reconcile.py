"""Tests for teatree.core.reconcile — state drift detector."""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.core.models import Ticket, Worktree
from teatree.core.reconcile import (
    Drift,
    EnvCacheDrift,
    MissingEnvCache,
    MissingWorktreeDir,
    reconcile_ticket,
)
from teatree.core.worktree_env import write_env_cache
from tests.teatree_core.conftest import CommandOverlay

_COMMAND = {"test": CommandOverlay()}


def _make(tmp: str, *, db_name: str = "wt_99") -> tuple[Ticket, Worktree, Path]:
    ticket_dir = Path(tmp) / "ticket-99"
    ticket_dir.mkdir()
    wt_path = ticket_dir / "backend"
    wt_path.mkdir()
    ticket = Ticket.objects.create(overlay="test", issue_url="https://ex.com/99", variant="acme")
    wt = Worktree.objects.create(
        overlay="test",
        ticket=ticket,
        repo_path="backend",
        branch="feature",
        db_name=db_name,
        extra={"worktree_path": str(wt_path)},
        state=Worktree.State.PROVISIONED,
    )
    return ticket, wt, wt_path


class TestDriftDataclass(TestCase):
    def test_has_drift_false_for_empty(self) -> None:
        drift = Drift(ticket_pk=1)
        assert not drift.has_drift
        assert drift.format() == "(no drift)"

    def test_has_drift_true_with_any_finding(self) -> None:
        drift = Drift(ticket_pk=1, missing_env_caches=[MissingEnvCache(worktree_pk=5, cache_path=Path("/x"))])
        assert drift.has_drift
        assert "missing-env-cache" in drift.format()


class TestReconcileTicket(TestCase):
    def test_detects_missing_env_cache(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND),
            patch("teatree.core.reconcile._find_docker_containers", return_value=[]),
            patch("teatree.core.reconcile._find_worktree_paths_on_disk", return_value=set()),
            patch("teatree.core.reconcile.db_exists", return_value=True),
        ):
            ticket, _, _ = _make(tmp)
            drift = reconcile_ticket(ticket)
        assert len(drift.missing_env_caches) == 1
        assert drift.missing_env_caches[0].worktree_pk
        assert drift.has_drift

    def test_detects_env_cache_drift(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND),
            patch("teatree.core.reconcile._find_docker_containers", return_value=[]),
            patch("teatree.core.reconcile._find_worktree_paths_on_disk", return_value=set()),
            patch("teatree.core.reconcile.db_exists", return_value=True),
        ):
            ticket, wt, _ = _make(tmp)
            spec = write_env_cache(wt)
            assert spec is not None
            # Tamper with the cache on disk.
            spec.path.chmod(0o644)
            spec.path.write_text("tampered\n", encoding="utf-8")
            drift = reconcile_ticket(ticket)
        assert len(drift.env_cache_drifts) == 1
        assert isinstance(drift.env_cache_drifts[0], EnvCacheDrift)

    def test_detects_missing_worktree_dir(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND),
            patch("teatree.core.reconcile._find_docker_containers", return_value=[]),
            patch("teatree.core.reconcile._find_worktree_paths_on_disk", return_value=set()),
            patch("teatree.core.reconcile.db_exists", return_value=True),
        ):
            ticket, wt, wt_path = _make(tmp)
            write_env_cache(wt)
            shutil.rmtree(wt_path)
            drift = reconcile_ticket(ticket)
        assert len(drift.missing_worktree_dirs) == 1
        assert isinstance(drift.missing_worktree_dirs[0], MissingWorktreeDir)

    def test_detects_orphan_containers_when_worktree_torn_down(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND),
            patch(
                "teatree.core.reconcile._find_docker_containers",
                return_value=["backend-wt99-web-1"],
            ),
            patch("teatree.core.reconcile._find_worktree_paths_on_disk", return_value=set()),
            patch("teatree.core.reconcile.db_exists", return_value=True),
        ):
            ticket, wt, _ = _make(tmp)
            wt.state = Worktree.State.CREATED  # post-teardown
            wt.save()
            drift = reconcile_ticket(ticket)
        assert len(drift.orphan_containers) == 1
        assert drift.orphan_containers[0].name == "backend-wt99-web-1"

    def test_clean_state_reports_no_drift(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND),
            patch("teatree.core.reconcile._find_docker_containers", return_value=[]),
            patch("teatree.core.reconcile._find_worktree_paths_on_disk", return_value=set()),
            patch("teatree.core.reconcile.db_exists", return_value=True),
        ):
            ticket, wt, _ = _make(tmp)
            write_env_cache(wt)
            drift = reconcile_ticket(ticket)
        assert not drift.has_drift
