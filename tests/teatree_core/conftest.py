"""Shared fixtures for teatree.core test modules."""

from collections.abc import Iterator
from typing import cast
from unittest.mock import patch

import pytest

from teatree.core.models import Worktree
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.core.overlay import OverlayBase, ProvisionStep, RunCommands
from teatree.core.overlay_loader import reset_overlay_cache


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


class CommandOverlay(OverlayBase):
    """Minimal overlay for management command tests."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        # Test double with no customer surface — the mandatory-E2E gate (#1967)
        # is inert here (matches the dogfood overlay's posture).
        _ = changed_files
        return False

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        def remember_setup() -> None:
            extra = cast("dict[str, str]", worktree.extra or {})
            extra["setup_hook"] = "ran"
            worktree.extra = extra
            worktree.save(update_fields=["extra"])

        return [ProvisionStep(name="remember-setup", callable=remember_setup)]

    def get_run_commands(self, worktree: Worktree) -> RunCommands:
        return {
            "backend": ["run-backend", worktree.repo_path],
            "frontend": ["run-frontend", worktree.repo_path],
        }

    def get_pre_run_steps(self, worktree: Worktree, service: str) -> list[ProvisionStep]:
        def remember_pre_run() -> None:
            extra = cast("dict[str, str]", worktree.extra or {})
            extra[f"pre_run_{service}"] = "ran"
            worktree.extra = extra
            worktree.save(update_fields=["extra"])

        return [ProvisionStep(name=f"pre-run-{service}", callable=remember_pre_run)]

    def get_e2e_env_extras(self, env_cache: dict[str, str]) -> dict[str, str]:
        variant = env_cache.get("WT_VARIANT", "")
        return {"CUSTOMER": variant} if variant else {}


COMMAND_OVERLAY = "tests.teatree_core.conftest.CommandOverlay"


@pytest.fixture(autouse=True)
def _clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


@pytest.fixture(autouse=True)
def _isolate_teatree_config(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Pin ``CONFIG_PATH`` to an empty config so autonomy defaults to ``babysit``.

    ``teatree.config.CONFIG_PATH`` freezes ``Path.home() / ".teatree.toml"`` at
    import time, so without this the merge-precondition tests would resolve the
    developer's real ``~/.teatree.toml`` (where ``t3-teatree`` may stand at
    ``autonomy = full``) and the substrate sign-off carve-out would change the
    held-vs-merged outcome under their feet. An empty config makes every overlay
    resolve to the conservative ``babysit`` default; a test that needs a
    specific tier opts in by patching ``CONFIG_PATH`` within its own scope.
    """
    empty = tmp_path_factory.mktemp("teatree-config") / ".teatree.toml"
    empty.write_text("[teatree]\n", encoding="utf-8")
    with patch("teatree.config.CONFIG_PATH", empty):
        yield


@pytest.fixture
def mock_command_overlay() -> Iterator[None]:
    """Patch _discover_overlays to return a CommandOverlay instance."""
    with patch(
        "teatree.core.overlay_loader._discover_overlays",
        return_value={"test": CommandOverlay()},
    ):
        yield
