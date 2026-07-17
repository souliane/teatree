"""Shared fixtures for teatree.core test modules."""

import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import override_settings

from teatree.core.models import Worktree
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.core.overlay import OverlayBase, OverlayE2E, OverlayReview, OverlayRuntime, ProvisionStep, RunCommands
from teatree.core.overlay_loader import reset_overlay_cache
from tests.db_alias import RouteAllToAlias, register_sqlite_alias, teardown_sqlite_alias


def seed_merge_safe_verdict(
    *,
    slug: str,
    pr_id: int,
    sha: str,
    reviewer: str = "cold-reviewer",
) -> ReviewVerdict:
    """Record the non-author MERGE_SAFE verdict the #2829 merge-verdict gate requires.

    ``execute_bound_merge`` now refuses any merge that lacks a non-stale
    independent ``merge_safe`` :class:`ReviewVerdict` at the live head. The
    production ``t3 <overlay> ticket clear`` path records exactly this verdict
    as a by-product of issuing the CLEAR; tests that build the CLEAR via
    ``MergeClear.issue`` / ``.objects.create`` (bypassing that command) seed it
    here so they still exercise the merge. Seeding is NOT a weakening — it
    reproduces what the real clear path records, with a non-author reviewer.
    """
    return ReviewVerdict.record(
        pr_id=pr_id,
        slug=slug,
        reviewed_sha=sha,
        verdict=ReviewVerdict.Verdict.MERGE_SAFE,
        reviewer_identity=reviewer,
    )


pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


class _CommandReview(OverlayReview):
    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        # Test double with no customer surface — the mandatory-E2E gate (#1967)
        # is inert here (matches the dogfood overlay's posture).
        _ = changed_files
        return False


class _CommandRuntime(OverlayRuntime):
    def run_commands(self, worktree: Worktree) -> RunCommands:
        return {
            "backend": ["run-backend", worktree.repo_path],
            "frontend": ["run-frontend", worktree.repo_path],
        }

    def pre_run_steps(self, worktree: Worktree, service: str) -> list[ProvisionStep]:
        def remember_pre_run() -> None:
            extra = cast("dict[str, str]", worktree.extra or {})
            extra[f"pre_run_{service}"] = "ran"
            worktree.extra = extra
            worktree.save(update_fields=["extra"])

        return [ProvisionStep(name=f"pre-run-{service}", callable=remember_pre_run)]


class _CommandE2E(OverlayE2E):
    def env_extras(self, env_cache: dict[str, str]) -> dict[str, str]:
        variant = env_cache.get("WT_VARIANT", "")
        return {"CUSTOMER": variant} if variant else {}


class CommandOverlay(OverlayBase):
    """Minimal overlay for management command tests."""

    review = _CommandReview()
    runtime = _CommandRuntime()
    e2e = _CommandE2E()

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        def remember_setup() -> None:
            extra = cast("dict[str, str]", worktree.extra or {})
            extra["setup_hook"] = "ran"
            worktree.extra = extra
            worktree.save(update_fields=["extra"])

        return [ProvisionStep(name="remember-setup", callable=remember_setup)]


COMMAND_OVERLAY = "tests.teatree_core.conftest.CommandOverlay"


@pytest.fixture(autouse=True)
def _clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


@pytest.fixture
def mock_command_overlay() -> Iterator[None]:
    """Patch _discover_overlays to return a CommandOverlay instance."""
    with patch(
        "teatree.core.overlay_loader._discover_overlays",
        return_value={"test": CommandOverlay()},
    ):
        yield


@dataclass(frozen=True)
class SchemaGuardAlias:
    """Factory for private, file-backed SQLite connections used by schema_guard tests (#2915)."""

    register_current: Callable[[], str]
    make_stale: Callable[[], str]


@pytest.fixture
def _unblocked_db(django_db_blocker: pytest.FixtureRequest) -> Iterator[None]:
    """Lift pytest-django's DB-access guard for a test that never touches ``default``."""
    with django_db_blocker.unblock():
        yield


@pytest.fixture
def schema_guard_alias(tmp_path: Path, _unblocked_db: None) -> Iterator[SchemaGuardAlias]:
    """Private, throwaway SQLite connections for schema_guard tests (#2915).

    Every alias this factory creates is registered against its own file under
    ``tmp_path`` and torn down automatically — a crashed reverse-migrate/
    restore cycle can corrupt only that one throwaway file, never the shared,
    xdist-worker-lifetime-reused ``default`` test database every other test in
    the worker relies on.

    The ``RouteAllToAlias`` router is installed for the alias's whole
    remaining lifetime, not just this factory's own migrate calls: the
    schema-guard functions under test (``migrate_self_db``,
    ``require_current_schema``) run their own ``migrate --database=<alias>``
    internally, and without the router active for *those* calls too, their
    RunPython seed/backfill operations resolve back onto the shared
    ``default`` connection they were built to avoid.
    """
    stack = ExitStack()
    created: list[str] = []

    def _register_current() -> str:
        """Register + migrate-to-HEAD a private, file-backed SQLite connection.

        Runs a real ``migrate`` (not a hand-rolled table) so the full app
        graph is current — the schema-guard functions under test read the
        migration ledger via Django's own ``MigrationExecutor``, which needs
        every app's history.
        """
        alias = f"sg_{uuid.uuid4().hex}"
        db_file = tmp_path / f"{alias}.sqlite3"
        register_sqlite_alias(alias, db_file)
        stack.enter_context(override_settings(DATABASE_ROUTERS=[RouteAllToAlias(alias)]))
        call_command("migrate", "--no-input", database=alias, verbosity=0)
        created.append(alias)
        return alias

    def _make_stale() -> str:
        """A private alias migrated to HEAD, then reverse-migrated ``core`` to ``zero``."""
        alias = _register_current()
        call_command("migrate", "core", "zero", "--no-input", database=alias, verbosity=0)
        return alias

    with stack:
        yield SchemaGuardAlias(register_current=_register_current, make_stale=_make_stale)

    for alias in created:
        teardown_sqlite_alias(alias)
