"""Regression test for #2919.

``worktree provision`` crashed on an unmigrated auto-isolated per-worktree
self-DB: the DB is seeded once from a canonical snapshot
(``teatree.paths._seed_isolated_db``) and never re-migrated on its own. A
provisioning step reads ``get_effective_settings()`` (e.g.
``provision_timebox.resolve_step_timeout_seconds``), and a stored
``ConfigSetting`` row that predates a since-added migration crashes that read
with a raw ``ValueError`` instead of provisioning. Migration
``0015_agent_harness_two_layer_config`` is the concrete reproduction: a
pre-#2887 ``agent_runtime`` row (``sdk_oauth`` / ``sdk_apikey`` / ``api``) is
unparsable by the current :class:`~teatree.config.enums.AgentRuntime` enum
until that migration's ``RunPython`` rewrites it.

``WorktreeProvisionRunner.run()`` now self-heals the self-DB schema first
(mirroring the sanctioned merge path's ``require_current_schema``, #2006),
so an unmigrated self-DB provisions cleanly instead of crashing.
"""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase

from teatree.config import get_effective_settings
from teatree.core.gates.schema_guard import SelfDbMigrationError, pending_migrations
from teatree.core.models import ConfigSetting, Ticket, Worktree
from teatree.core.overlay import DbImportStrategy, OverlayBase, ProvisionStep
from teatree.core.runners import WorktreeProvisionRunner


def _migrate_core_to(target: str) -> None:
    call_command("migrate", "core", target, "--no-input", verbosity=0)


def _migrate_core_forward() -> None:
    call_command("migrate", "core", "--no-input", verbosity=0)


class _DbImportOverlay(OverlayBase):
    """Overlay whose DB-import strategy routes through ``run_timeboxed_db_import``."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []

    def get_db_import_strategy(self, worktree: Worktree) -> DbImportStrategy | None:
        return {
            "kind": "fallback-chain",
            "source_database": "dev",
            "shared_postgres": True,
            "snapshot_tool": "dslr",
            "restore_order": [],
            "notes": [],
            "worktree_repo_path": worktree.repo_path,
        }

    def db_import(self, worktree: Worktree, **kwargs: Any) -> bool:
        return True


# ``setUp`` reverse-migrates ``core`` to a mid-graph target and the cleanup runs
# a full forward heal on the shared ``default`` connection — several seconds
# single-core that exceeds the global 60s ``pytest-timeout`` under maximum
# ``-n auto --cov --doctest-modules`` parallel contention. Scoped 240s bump for
# the genuinely-slow migrations; the global 60s stays the hang-detector (#1189).
@pytest.mark.timeout(240)
class WorktreeProvisionSelfHealsStaleAgentRuntimeTest(TransactionTestCase):
    """Self-DB left one migration behind, carrying a pre-#2887 ``agent_runtime`` row."""

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.wt_path = tmp_path / "worktree"
        self.wt_path.mkdir()

    def setUp(self) -> None:
        _migrate_core_to("0014_ticket_repo_namespaced_key")
        ConfigSetting.objects.create(scope="", key="agent_runtime", value="sdk_oauth")
        self.addCleanup(_migrate_core_forward)

    def _make_worktree(self) -> Worktree:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/i/2919")
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="backend",
            branch="feature",
            db_name="wt_2919",
            extra={"worktree_path": str(self.wt_path)},
        )

    def test_stale_row_crashes_settings_resolution_while_unmigrated(self) -> None:
        assert pending_migrations(), "guard precondition: migration 0015 must still be pending"
        with pytest.raises(ValueError, match="agent_runtime"):
            get_effective_settings()

    def test_provision_runner_self_heals_before_reading_settings(self) -> None:
        worktree = self._make_worktree()
        overlay = _DbImportOverlay()

        with (
            patch(
                "teatree.core.runners.worktree_provision._setup_worktree_dir",
                return_value=None,
            ),
            patch("teatree.utils.db.db_exists", return_value=False),
        ):
            result = WorktreeProvisionRunner(worktree, overlay=overlay).run()

        assert result.ok is True, result.detail
        assert pending_migrations() == []
        # 0015's RunPython collapsed the stale value in place.
        assert get_effective_settings().agent_runtime.value == "headless"


class WorktreeProvisionFailsLoudOnSelfHealFailureTest(TestCase):
    """A self-heal migrate failure must fail loud, never crash with a raw traceback."""

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.wt_path = tmp_path / "worktree"
        self.wt_path.mkdir()

    def test_reports_the_migrate_failure_instead_of_raising(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/i/2919b")
        worktree = Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="backend",
            branch="feature",
            db_name="wt_2919b",
            extra={"worktree_path": str(self.wt_path)},
        )

        with patch(
            "teatree.core.runners.worktree_provision.require_current_schema",
            side_effect=SelfDbMigrationError("migrate exploded mid-apply"),
        ):
            result = WorktreeProvisionRunner(worktree, overlay=_DbImportOverlay()).run()

        assert result.ok is False
        assert "migrate exploded mid-apply" in result.detail
