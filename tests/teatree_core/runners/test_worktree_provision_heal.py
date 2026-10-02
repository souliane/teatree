"""The missing-DB heal repairs an INTERRUPTED PROVISION — never a running stack.

#1038's heal exists for one shape: a provision killed between the FSM flip to
``PROVISIONED`` and the DB import, leaving a row whose ``db_name`` names a
database that was never created. Its only guards were "has a db_name" and "the
overlay imports databases", so it also fired on a ``SERVICES_UP`` row whose
``db_name`` had drifted away from the database its stack is actually using — and
the repair is a DB import, which drops and re-imports from a snapshot.

That is destructive repair from a premise nobody validated: the drifted name is
evidence the ROW is wrong, not the database. A running stack's data is the least
safe thing in the system to rebuild from a snapshot on that evidence.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import DbImportStrategy, OverlayBase, OverlayProvisioning, OverlayReview, ProvisionStep
from teatree.core.runners import worktree_provision as provision_mod
from teatree.core.runners.worktree_provision import heal_missing_provisioned_db


class _ImportingProvisioning(OverlayProvisioning):
    def db_import_strategy(self, worktree: Worktree) -> DbImportStrategy | None:
        _ = worktree
        return {"shared_postgres": True}


class _HealReview(OverlayReview):
    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        _ = changed_files
        return False


class _ImportingOverlay(OverlayBase):
    review = _HealReview()
    provisioning = _ImportingProvisioning()

    def get_repos(self) -> list[str]:
        return ["repo"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        _ = worktree
        return []


def _worktree(*, state: Worktree.State) -> Worktree:
    ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/42")
    return Worktree.objects.create(
        ticket=ticket,
        overlay="test",
        repo_path="repo",
        branch="42-feat",
        state=state,
        db_name="wt_155",
        compose_project="repo-wt155",
        extra={"worktree_path": "/tmp/wt"},
    )


class TestHealIsScopedToProvisioned(TestCase):
    def test_a_running_stack_is_never_re_imported_under_itself(self) -> None:
        worktree = _worktree(state=Worktree.State.SERVICES_UP)
        with (
            patch.object(provision_mod, "WorktreeProvisionRunner") as runner,
            patch("teatree.utils.db.db_exists", return_value=False),
            patch.object(provision_mod, "worktree_pg_connection", return_value=("u", "h", {})),
        ):
            healed = heal_missing_provisioned_db(worktree, _ImportingOverlay())
        assert not healed
        runner.assert_not_called()

    def test_a_ready_stack_is_never_re_imported_under_itself(self) -> None:
        worktree = _worktree(state=Worktree.State.READY)
        with (
            patch.object(provision_mod, "WorktreeProvisionRunner") as runner,
            patch("teatree.utils.db.db_exists", return_value=False),
            patch.object(provision_mod, "worktree_pg_connection", return_value=("u", "h", {})),
        ):
            healed = heal_missing_provisioned_db(worktree, _ImportingOverlay())
        assert not healed
        runner.assert_not_called()

    def test_an_interrupted_provision_still_heals(self) -> None:
        worktree = _worktree(state=Worktree.State.PROVISIONED)
        with (
            patch.object(provision_mod, "WorktreeProvisionRunner") as runner,
            patch("teatree.utils.db.db_exists", return_value=False),
            patch.object(provision_mod, "worktree_pg_connection", return_value=("u", "h", {})),
        ):
            runner.return_value.run.return_value = provision_mod.RunnerResult(ok=True, detail="re-provisioned")
            healed = heal_missing_provisioned_db(worktree, _ImportingOverlay())
        assert healed
        runner.assert_called_once()
