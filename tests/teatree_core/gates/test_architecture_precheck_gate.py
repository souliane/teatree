"""Warn-only architecture pre-check gate on PR creation.

Doctrine (skills/architecture-design § "The ten checks" + § "Scope discipline"):
an architecture-touching change carries a ``## Architecture pre-check`` section
in the PR body, and every required check — removability (#10) included — must
carry a real answer. This gate runs the ``teatree.quality.architecture_precheck``
validator against the PR body: a section with an unanswered required check WARNS
(never hard-fails — repo doctrine: a gate without a reliable heuristic warns);
a body with no section, or a fully-answered section, is silent.

The ``TestShipExecutorEmitsWarn`` / ``TestOrphanPathEmitsWarn`` cases prove the
warn fires on a REAL PR-creation invocation, not only the unit helper.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.gates.architecture_precheck_gate import (
    ARCHITECTURE_PRECHECK_HINT,
    has_precheck_section,
    warn_if_precheck_incomplete,
)
from teatree.core.management.commands import _ensure_pr as ensure_pr_mod
from teatree.core.management.commands._ensure_pr import create_or_defer_pr
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import reset_overlay_cache
from teatree.core.runners.ship import ShipExecutor
from tests.teatree_core.conftest import CommandOverlay

_GATE_LOGGER = "teatree.core.gates.architecture_precheck_gate"

_ANSWERED_1_TO_9 = """## Architecture pre-check — teatree#2743

## 1. BLUEPRINT § alignment
§5.2 phase dispatch.

## 2. FSM phase boundaries
n/a — no transition.

## 3. Extension-point contracts
None.

## 4. Component boundaries
core/gates owns the wiring.

## 5. Dependency direction
core -> quality, downward; tach green.

## 6. Test surface
this file asserts the warn fires.

## 7. Resilience invariants
n/a — no external write.

## 8. Identity and key normalization
n/a.

## 9. Behavior preservation / capability deletion
n/a — purely additive.
"""

_REMOVABILITY = """
## 10. Removability / harness-vs-data
Removable — delete the module + two call lines. Lives in the harness.
"""

_COMPLETE = _ANSWERED_1_TO_9 + _REMOVABILITY


class TestHasPrecheckSection:
    @pytest.mark.parametrize(
        "body",
        [
            "feat: x\n\n## Architecture pre-check — teatree#42\n\n## 1. ...",
            "feat: x\n\n### Architecture Pre-Check\n- ...",
            "feat: x\n\n# architecture precheck\n- ...",
        ],
    )
    def test_detects_section_heading_variants(self, body: str) -> None:
        assert has_precheck_section(body) is True

    @pytest.mark.parametrize(
        "body",
        [
            "feat: x\n\nJust a plain body with no section.",
            "",
            "feat: x\n\n## Summary\n- did a thing",
            "mentions architecture pre-check inline but no heading line",
        ],
    )
    def test_missing_section_not_detected(self, body: str) -> None:
        assert has_precheck_section(body) is False


class TestWarnIfPrecheckIncomplete:
    def test_warns_and_names_removability_when_check_10_absent(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=_GATE_LOGGER):
            message = warn_if_precheck_incomplete(_ANSWERED_1_TO_9)
        assert message is not None
        assert "Removability" in message
        assert ARCHITECTURE_PRECHECK_HINT in message
        assert any("Removability" in record.message for record in caplog.records)

    def test_silent_when_all_ten_answered(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=_GATE_LOGGER):
            message = warn_if_precheck_incomplete(_COMPLETE)
        assert message is None
        assert caplog.records == []

    def test_silent_when_no_precheck_section(self, caplog: pytest.LogCaptureFixture) -> None:
        # A tactical change with a freeform body must NOT trip the all-unanswered
        # result of precheck_findings — no section means nothing to validate.
        with caplog.at_level(logging.WARNING, logger=_GATE_LOGGER):
            message = warn_if_precheck_incomplete("feat: tweak\n\nOne-line string change.")
        assert message is None
        assert caplog.records == []


_MOCK_OVERLAY = {"test": CommandOverlay()}


class TestShipExecutorEmitsWarn(TestCase):
    def setUp(self) -> None:
        reset_overlay_cache()
        self.addCleanup(reset_overlay_cache)

    def _ticket_with_worktree(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/2743")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/wt",
            branch="feat-x",
            extra={"worktree_path": "/tmp/wt"},
        )
        return ticket

    def _run_ship(self, body: str) -> MagicMock:
        ticket = self._ticket_with_worktree()
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://example.com/mr/1"}
        host.current_user.return_value = "dev"
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push"),
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: x", body)),
        ):
            result = ShipExecutor(ticket).run()
        assert result.ok is True
        return host

    def test_warns_when_pr_body_precheck_missing_removability(self) -> None:
        with self.assertLogs(_GATE_LOGGER, level="WARNING") as logs:
            host = self._run_ship(_ANSWERED_1_TO_9)
        host.create_pr.assert_called_once()
        assert any("Removability" in line for line in logs.output)

    def test_silent_when_pr_body_precheck_complete(self) -> None:
        logger = logging.getLogger(_GATE_LOGGER)
        with patch.object(logger, "warning") as mock_warning:
            host = self._run_ship(_COMPLETE)
        host.create_pr.assert_called_once()
        mock_warning.assert_not_called()


class TestOrphanPathEmitsWarn(TestCase):
    def setUp(self) -> None:
        reset_overlay_cache()
        self.addCleanup(reset_overlay_cache)

    def _create_orphan_pr(self, body: str) -> MagicMock:
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://example.com/pr/1"}
        host.current_user.return_value = "dev"
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(ensure_pr_mod, "code_host_for_repo_from_overlay", return_value=host),
            patch.object(ensure_pr_mod, "_branch_own_commit_message", return_value=("feat: x", body)),
            patch.object(ensure_pr_mod, "_ticket_extra_for_branch", return_value=None),
            patch.object(ensure_pr_mod.git, "remote_url", return_value="git@github.com:souliane/teatree.git"),
        ):
            create_or_defer_pr(".", "feat-x")
        return host

    def test_warns_when_orphan_pr_body_precheck_missing_removability(self) -> None:
        with self.assertLogs(_GATE_LOGGER, level="WARNING") as logs:
            host = self._create_orphan_pr(_ANSWERED_1_TO_9)
        host.create_pr.assert_called_once()
        assert any("Removability" in line for line in logs.output)

    def test_silent_when_orphan_pr_body_precheck_complete(self) -> None:
        logger = logging.getLogger(_GATE_LOGGER)
        with patch.object(logger, "warning") as mock_warning:
            host = self._create_orphan_pr(_COMPLETE)
        host.create_pr.assert_called_once()
        mock_warning.assert_not_called()
