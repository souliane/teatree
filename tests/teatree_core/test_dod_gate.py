"""Definition-of-Done gate: a UI-visible ticket needs a local-stack E2E (#88).

The gate refuses the ``ship()`` transition for a ticket whose change is
visible in the UI (a frontend repo is in scope) when no green local-stack
E2E artifact exists, mirroring the existing dirty-worktree preflight. A
deferred dev-after-merge run does NOT satisfy it. An explicit recorded
override is the escape hatch so the gate can never hard-trap a legitimate
non-UI or exempt ticket.

The pure checks (``is_ui_visible``, ``has_local_e2e_artifact``,
``override_reason``, ``check_local_e2e_dod``) are exercised directly; the
FSM-integration test drives the real ``ship()`` transition and is proven
anti-vacuous by ``test_gate_is_load_bearing_for_ship`` — with the gate
removed, the UI-visible no-E2E ship advances.
"""

from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.e2e_workitem import record_run
from teatree.core.gates import dod_gate
from teatree.core.gates.dod_gate import (
    DodLocalE2EError,
    check_local_e2e_dod,
    has_local_e2e_artifact,
    is_ui_visible,
    override_reason,
)
from teatree.core.models import Ticket, Worktree
from tests.teatree_core.models._shared import _advance_ticket_to_tested, _complete_phase_task, _init_repo_with_branch


def _patch_overlay(frontend_repos: list[str]) -> AbstractContextManager[MagicMock]:
    """Patch the frontend-repo resolution seam ``_frontend_repos`` delegates to."""
    return patch.object(dod_gate, "frontend_repos_for_overlay", return_value=list(frontend_repos))


class TestIsUiVisible(TestCase):
    def test_ticket_with_frontend_repo_in_scope_is_ui_visible(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", repos=["acme-backend", "acme-frontend"])
        with _patch_overlay(["acme-frontend"]):
            assert is_ui_visible(ticket) is True

    def test_backend_only_ticket_is_not_ui_visible(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", repos=["acme-backend"])
        with _patch_overlay(["acme-frontend"]):
            assert is_ui_visible(ticket) is False

    def test_overlay_with_no_frontend_repos_is_never_ui_visible(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", repos=["acme-frontend"])
        with _patch_overlay([]):
            assert is_ui_visible(ticket) is False

    def test_unresolvable_overlay_fails_closed_to_presumed_ui_visible(self) -> None:
        """#1426: an unresolvable overlay must FAIL CLOSED — presume UI-visible.

        The safety gate must not be silently skipped on a misconfigured instance.
        """
        from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="missing", repos=["acme-frontend"])
        with patch.object(dod_gate, "frontend_repos_for_overlay", side_effect=ImproperlyConfigured("no overlay")):
            assert is_ui_visible(ticket) is True

    def test_no_scoped_repos_is_not_ui_visible_even_when_overlay_undeterminable(self) -> None:
        """A ticket with no scoped repos cannot be UI-visible, so fail-closed does not apply.

        Nothing in an empty repo set can intersect ``frontend_repos``; the
        fail-closed branch is reserved for the ambiguous "repos exist but
        cannot be classified" case.
        """
        from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="missing", repos=[])
        with patch.object(dod_gate, "frontend_repos_for_overlay", side_effect=ImproperlyConfigured("no overlay")):
            assert is_ui_visible(ticket) is False


class TestHasLocalE2EArtifact(TestCase):
    def test_green_local_run_satisfies(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/i/1")
        record_run(ticket, result="green", per_repo_shas={"acme-frontend": "sha"}, env="local")
        assert has_local_e2e_artifact(ticket) is True

    def test_green_dev_run_does_not_satisfy(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/i/2")
        record_run(ticket, result="green", per_repo_shas={"acme-frontend": "sha"}, env="dev")
        assert has_local_e2e_artifact(ticket) is False

    def test_red_local_run_does_not_satisfy(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/i/3")
        record_run(ticket, result="red", per_repo_shas={"acme-frontend": "sha"}, env="local")
        assert has_local_e2e_artifact(ticket) is False

    def test_run_with_no_env_does_not_satisfy(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/i/4")
        ticket.merge_extra(set_keys={"e2e_recipe": {"repos": [], "last_run": {"result": "green"}}})
        assert has_local_e2e_artifact(ticket) is False

    def test_no_recipe_does_not_satisfy(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/i/5")
        assert has_local_e2e_artifact(ticket) is False

    def test_malformed_non_mapping_last_run_does_not_crash(self) -> None:
        """A non-mapping ``last_run`` (corrupt durable JSON) is no valid artifact, not a raise."""
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/i/6")
        ticket.merge_extra(set_keys={"e2e_recipe": {"repos": [], "last_run": "garbage"}})
        assert has_local_e2e_artifact(ticket) is False

    def test_malformed_list_last_run_does_not_crash(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/i/7")
        ticket.merge_extra(set_keys={"e2e_recipe": {"repos": [], "last_run": ["not", "a", "mapping"]}})
        assert has_local_e2e_artifact(ticket) is False


class TestOverrideReason(TestCase):
    def test_returns_recorded_reason(self) -> None:
        ticket = Ticket.objects.create(overlay="acme")
        ticket.merge_extra(set_keys={"dod_e2e_override": {"reason": "non-UI config change"}})
        assert override_reason(ticket) == "non-UI config change"

    def test_returns_empty_when_no_override(self) -> None:
        ticket = Ticket.objects.create(overlay="acme")
        assert override_reason(ticket) == ""

    def test_blank_reason_is_treated_as_no_override(self) -> None:
        ticket = Ticket.objects.create(overlay="acme")
        ticket.merge_extra(set_keys={"dod_e2e_override": {"reason": "   "}})
        assert override_reason(ticket) == ""


class TestCheckLocalE2EDod(TestCase):
    def test_passes_when_not_ui_visible(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", repos=["acme-backend"])
        with _patch_overlay(["acme-frontend"]):
            check_local_e2e_dod(ticket)  # no raise

    def test_blocks_ui_visible_without_local_e2e(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", repos=["acme-frontend"])
        with _patch_overlay(["acme-frontend"]), pytest.raises(DodLocalE2EError) as exc:
            check_local_e2e_dod(ticket)
        assert "local-stack E2E" in str(exc.value)
        assert "dod-override" in str(exc.value)  # message names the escape hatch

    def test_passes_ui_visible_with_green_local_e2e(self) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://example.com/i/10",
            repos=["acme-frontend"],
        )
        record_run(ticket, result="green", per_repo_shas={"acme-frontend": "sha"}, env="local")
        with _patch_overlay(["acme-frontend"]):
            check_local_e2e_dod(ticket)  # no raise

    def test_blocks_ui_visible_with_only_dev_e2e(self) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://example.com/i/11",
            repos=["acme-frontend"],
        )
        record_run(ticket, result="green", per_repo_shas={"acme-frontend": "sha"}, env="dev")
        with _patch_overlay(["acme-frontend"]), pytest.raises(DodLocalE2EError):
            check_local_e2e_dod(ticket)

    def test_override_unblocks_ui_visible_without_e2e(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", repos=["acme-frontend"])
        ticket.merge_extra(set_keys={"dod_e2e_override": {"reason": "exempt: backend-only despite repo set"}})
        with _patch_overlay(["acme-frontend"]):
            check_local_e2e_dod(ticket)  # no raise

    def test_blocks_when_overlay_undeterminable_and_no_e2e_or_override(self) -> None:
        """#1426 fail-closed: an undeterminable overlay presumes UI-visible and the gate fires."""
        from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="missing", repos=["acme-frontend"])
        with (
            patch.object(dod_gate, "frontend_repos_for_overlay", side_effect=ImproperlyConfigured("no overlay")),
            pytest.raises(DodLocalE2EError),
        ):
            check_local_e2e_dod(ticket)

    def test_undeterminable_overlay_is_not_a_lockout_with_override(self) -> None:
        """Never-lockout: the override escape hatch still passes under fail-closed."""
        from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="missing", repos=["acme-frontend"])
        ticket.merge_extra(set_keys={"dod_e2e_override": {"reason": "exempt config-only change"}})
        with patch.object(dod_gate, "frontend_repos_for_overlay", side_effect=ImproperlyConfigured("no overlay")):
            check_local_e2e_dod(ticket)  # no raise

    def test_undeterminable_overlay_is_not_a_lockout_with_green_e2e(self) -> None:
        """Never-lockout: a green local E2E still passes under fail-closed."""
        from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="missing", issue_url="https://example.com/i/12", repos=["acme-frontend"])
        record_run(ticket, result="green", per_repo_shas={"acme-frontend": "sha"}, env="local")
        with patch.object(dod_gate, "frontend_repos_for_overlay", side_effect=ImproperlyConfigured("no overlay")):
            check_local_e2e_dod(ticket)  # no raise

    def test_path_only_toml_overlay_does_not_fail_closed_for_backend_ticket(self) -> None:
        """A path-only TOML overlay (no Python ``class``) resolves from its config table.

        The regression (#733): ``get_overlay`` cannot instantiate a path-only
        overlay in the teatree process, so the gate failed closed and refused
        the ship for EVERY one of its tickets. With resolution delegated to
        ``frontend_repos_for_overlay``, a path-only overlay with no declared
        ``frontend_repos`` resolves to an empty list — the ticket is
        backend-from-teatree's-view and the gate passes, instead of presuming
        UI-visible and blocking.
        """
        ticket = Ticket.objects.create(overlay="path-only-ovl", repos=["some-repo"])
        with patch.object(dod_gate, "frontend_repos_for_overlay", return_value=[]):
            assert is_ui_visible(ticket) is False
            check_local_e2e_dod(ticket)  # no raise


class TestShipTransitionDodGate(TestCase):
    """The real FSM ``ship()`` path enforces the gate (#88)."""

    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _reviewed_ui_ticket(self) -> Worktree:
        ticket = Ticket.objects.create()
        repo_dir = self._tmp_path / f"repo-{ticket.pk}"
        branch = f"feature-{ticket.pk}"
        _init_repo_with_branch(repo_dir, branch=branch, commits_ahead=1)
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path=str(repo_dir),
            branch=branch,
            extra={"worktree_path": str(repo_dir)},
        )
        # _advance_ticket_to_tested scopes repos=["backend", "frontend"].
        _advance_ticket_to_tested(ticket)
        _complete_phase_task(ticket, "reviewing")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED
        return wt

    def test_ship_refused_for_ui_visible_ticket_without_local_e2e(self) -> None:
        wt = self._reviewed_ui_ticket()
        ticket = wt.ticket
        with _patch_overlay(["frontend"]), pytest.raises(DodLocalE2EError) as exc:
            ticket.ship()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED  # ship did NOT advance
        assert "local-stack E2E" in str(exc.value)

    def test_ship_proceeds_with_green_local_e2e(self) -> None:
        wt = self._reviewed_ui_ticket()
        ticket = wt.ticket
        record_run(ticket, result="green", per_repo_shas={"frontend": "sha"}, env="local")
        with _patch_overlay(["frontend"]), self.captureOnCommitCallbacks(execute=False):
            ticket.ship()
            ticket.save()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED

    def test_ship_proceeds_for_backend_only_ticket(self) -> None:
        wt = self._reviewed_ui_ticket()
        ticket = wt.ticket
        with _patch_overlay(["some-other-frontend"]), self.captureOnCommitCallbacks(execute=False):
            # The ticket's repos (backend, frontend) do not intersect the
            # overlay's frontend repos here, so it is not UI-visible.
            ticket.ship()
            ticket.save()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED

    def test_ship_proceeds_with_recorded_override(self) -> None:
        wt = self._reviewed_ui_ticket()
        ticket = wt.ticket
        ticket.merge_extra(set_keys={"dod_e2e_override": {"reason": "exempt feature-flag-gated rollout"}})
        with _patch_overlay(["frontend"]), self.captureOnCommitCallbacks(execute=False):
            ticket.ship()
            ticket.save()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED

    def test_gate_is_load_bearing_for_ship(self) -> None:
        """Anti-vacuity: with the gate neutralised, the blocked ship advances.

        Re-introduces the pre-#88 behaviour (the gate is a no-op) and
        confirms the same UI-visible no-E2E ticket the block test refuses
        now ships. If this passes while ``test_ship_refused_...`` also
        passes, the gate is genuinely the thing doing the blocking.
        """
        wt = self._reviewed_ui_ticket()
        ticket = wt.ticket
        with (
            _patch_overlay(["frontend"]),
            patch.object(dod_gate, "check_local_e2e_dod", return_value=None),
            self.captureOnCommitCallbacks(execute=False),
        ):
            ticket.ship()
            ticket.save()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
