"""Tests for teatree.core.reconcile — state drift detector."""

import shutil
import tempfile
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from django.test import TestCase

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayBase
from teatree.core.reconcile import (
    Drift,
    EnvCacheDrift,
    MissingEnvCache,
    MissingWorktreeDir,
    UnresolvableOverlay,
    _collect_stale_worktree_dirs,
    reconcile_all,
    reconcile_ticket,
)
from teatree.core.worktree_env import write_env_cache
from tests.teatree_core.conftest import CommandOverlay

_COMMAND = {"test": CommandOverlay()}


def _make_ghost(tmp: str, *, dir_name: str = "ticket-ghost") -> tuple[Ticket, Worktree, Path]:
    """A provisioned worktree whose overlay name is not registered anywhere.

    ``get_overlay_for_worktree`` raises ``ImproperlyConfigured`` for it, the
    same way a row for an overlay uninstalled in this environment does.
    """
    ticket_dir = Path(tmp) / dir_name
    ticket_dir.mkdir()
    wt_path = ticket_dir / "backend"
    wt_path.mkdir()
    ticket = Ticket.objects.create(overlay="t3-ghost", issue_url="https://ex.com/ghost")
    wt = Worktree.objects.create(
        overlay="t3-ghost",
        ticket=ticket,
        repo_path="backend",
        branch="ghost",
        db_name="",
        extra={"worktree_path": str(wt_path)},
        state=Worktree.State.PROVISIONED,
    )
    return ticket, wt, wt_path


class _PgUserOverlay(CommandOverlay):
    """Overlay that connects to postgres as a non-default superuser role."""

    def get_env_extra(self, worktree: Worktree) -> dict[str, str]:
        return {"POSTGRES_USER": "db_superuser", "POSTGRES_HOST": "localhost"}


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


class TestReconcileMissingDbUsesWorktreePgUser(TestCase):
    """An existing DB reachable only as the overlay's role must not read as missing.

    The bug: ``db_exists`` connected with the bare process-env default
    ``POSTGRES_USER`` (``postgres`` — a role that need not exist), so a
    DB owned by a non-default superuser role reported missing for many
    tickets. ``doctor --fix`` then nudges a re-provision that drops the
    good DB. The reconciler must connect with the worktree's resolved role.
    """

    _OVERLAYS: ClassVar[dict[str, OverlayBase]] = {"test": _PgUserOverlay()}

    def _existing_only_as_superuser(self, db_name: str, *, user: str = "", **_: object) -> bool:
        # Mirrors the host: the DB exists, but only the overlay's role can see it.
        # The default ``postgres`` connection fails and yields no rows -> False.
        return user == "db_superuser" and db_name == "wt_99"

    def test_existing_db_not_reported_missing_when_owned_by_overlay_role(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=self._OVERLAYS),
            patch("teatree.core.reconcile._find_docker_containers", return_value=[]),
            patch("teatree.core.reconcile._find_worktree_paths_on_disk", return_value=set()),
            patch("teatree.core.reconcile.db_exists", side_effect=self._existing_only_as_superuser),
        ):
            ticket, wt, _ = _make(tmp)
            write_env_cache(wt)
            drift = reconcile_ticket(ticket)
        assert drift.missing_dbs == []

    def test_genuinely_absent_db_still_reported_missing(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=self._OVERLAYS),
            patch("teatree.core.reconcile._find_docker_containers", return_value=[]),
            patch("teatree.core.reconcile._find_worktree_paths_on_disk", return_value=set()),
            patch("teatree.core.reconcile.db_exists", return_value=False),
        ):
            ticket, wt, _ = _make(tmp)
            write_env_cache(wt)
            drift = reconcile_ticket(ticket)
        assert len(drift.missing_dbs) == 1
        assert drift.missing_dbs[0].db_name == "wt_99"


class TestReconcileUnresolvableOverlay(TestCase):
    """A row whose overlay is not installed here must not abort the sweep (#2472)."""

    _PATCHES: ClassVar[tuple] = ()

    def _patches(self):
        return (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND),
            patch("teatree.core.reconcile._find_docker_containers", return_value=[]),
            patch("teatree.core.reconcile._find_worktree_paths_on_disk", return_value=set()),
            patch("teatree.core.reconcile.db_exists", return_value=True),
        )

    def test_records_unresolvable_overlay_instead_of_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for ctx in self._patches():
                self.enterContext(ctx)
            ticket, wt, _ = _make_ghost(tmp)
            drift = reconcile_ticket(ticket)
        assert len(drift.unresolvable_overlays) == 1
        assert isinstance(drift.unresolvable_overlays[0], UnresolvableOverlay)
        assert drift.unresolvable_overlays[0].worktree_pk == wt.pk
        assert drift.unresolvable_overlays[0].overlay == "t3-ghost"
        assert drift.has_drift
        assert "unresolvable-overlay" in drift.format()

    def test_still_detects_missing_dir_for_unresolvable_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for ctx in self._patches():
                self.enterContext(ctx)
            ticket, _, wt_path = _make_ghost(tmp)
            shutil.rmtree(wt_path)
            drift = reconcile_ticket(ticket)
        assert len(drift.missing_worktree_dirs) == 1
        assert len(drift.unresolvable_overlays) == 1

    def test_reconcile_all_isolates_the_unresolvable_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for ctx in self._patches():
                self.enterContext(ctx)
            ghost_ticket, _, _ = _make_ghost(tmp)
            ok_ticket, ok_wt, ok_path = _make(tmp)
            write_env_cache(ok_wt)
            shutil.rmtree(ok_path)
            drifts = reconcile_all()
        # The ghost ticket is surfaced (unresolvable) AND the installed-overlay
        # ticket is still reconciled — the sweep no longer aborts on the ghost.
        assert ghost_ticket.pk in drifts
        assert ok_ticket.pk in drifts
        assert len(drifts[ok_ticket.pk].missing_worktree_dirs) == 1


class TestStaleWorktreeDirAttributionIsSegmentAnchored(TestCase):
    """Stale-dir attribution anchors the ticket-number on path segments (#WT-PR-D finding 17).

    ``/9`` must not match ``/90``: the pre-fix raw substring (``f"/{n}" in path``)
    mis-attributed an unrelated ticket-90 dir to ticket 9.
    """

    def _ticket9_with_wt(self) -> tuple[Ticket, Worktree]:
        ticket = Ticket.objects.create(issue_url="https://github.com/org/repo/issues/9")
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path="repo",
            branch="9-fix",
            extra={"worktree_path": "/ws/9-fix/repo"},
        )
        return ticket, wt

    def test_ticket90_dir_not_attributed_to_ticket9(self) -> None:
        ticket, wt = self._ticket9_with_wt()
        drift = Drift(ticket_pk=ticket.pk)
        foreign = "/ws/90-other/repo"  # belongs to ticket 90, not 9
        with (
            patch("teatree.core.reconcile._find_worktree_paths_on_disk", return_value={foreign}),
            patch("teatree.core.reconcile.resolve_clone_path", return_value=Path("/ws/repo")),
        ):
            _collect_stale_worktree_dirs(drift, [wt], ticket, Path("/ws"))

        assert drift.stale_worktree_dirs == []

    def test_genuine_ticket9_dir_is_attributed(self) -> None:
        ticket, wt = self._ticket9_with_wt()
        drift = Drift(ticket_pk=ticket.pk)
        genuine = "/ws/9-elsewhere/repo"  # a stale dir genuinely for ticket 9
        with (
            patch("teatree.core.reconcile._find_worktree_paths_on_disk", return_value={genuine}),
            patch("teatree.core.reconcile.resolve_clone_path", return_value=Path("/ws/repo")),
        ):
            _collect_stale_worktree_dirs(drift, [wt], ticket, Path("/ws"))

        assert len(drift.stale_worktree_dirs) == 1
        assert str(drift.stale_worktree_dirs[0].path) == genuine
