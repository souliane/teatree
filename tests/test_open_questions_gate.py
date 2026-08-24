"""Warn-only Open-questions-section gate on PR creation (souliane/teatree#1933).

Doctrine (skills/ship § "Open Questions & Assumptions"): any open question and
any non-explicit assumption must be listed in both the commit message body and
the PR description under an "Open questions & assumptions" section. This gate is
the smallest deterministic enforcement artifact: when a PR body lacks the
section heading, the gate WARNS (never hard-fails — repo doctrine: a gate
without a reliable heuristic warns) with a hint. A body that carries the
heading is silent.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.gates.open_questions_gate import (
    OPEN_QUESTIONS_HINT,
    RATIFICATION_HINT,
    cited_ratification_artifacts,
    has_open_questions_section,
    warn_if_open_questions_missing,
    warn_if_owner_ratification_unbacked,
)
from teatree.core.management.commands import _ensure_pr as ensure_pr_mod
from teatree.core.management.commands._ensure_pr import create_or_defer_pr
from teatree.core.models import PlanArtifact, Ticket, Worktree
from teatree.core.overlay_loader import reset_overlay_cache
from teatree.core.runners.ship import ShipExecutor
from tests.teatree_core.conftest import CommandOverlay


class TestHasOpenQuestionsSection:
    @pytest.mark.parametrize(
        "body",
        [
            "feat: x\n\n## Open questions & assumptions\n\n- none",
            "feat: x\n\n## Open Questions & Assumptions\n- assumed: foo",
            "feat: x\n\n# Open questions\n- decided-by-user: bar",
            "feat: x\n\nOpen questions & assumptions:\n- open: baz",
            "body\n\n### Open Questions\n- assumed: y",
        ],
    )
    def test_detects_section_heading_variants(self, body: str) -> None:
        assert has_open_questions_section(body) is True

    @pytest.mark.parametrize(
        "body",
        [
            "feat: x\n\nJust a plain body with no section.",
            "",
            "feat: x\n\n## Summary\n- did a thing",
            "mentions open questions inline but no heading line",
        ],
    )
    def test_missing_section_not_detected(self, body: str) -> None:
        assert has_open_questions_section(body) is False


class TestWarnIfMissing:
    def test_warns_and_returns_message_when_missing(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="teatree.core.gates.open_questions_gate"):
            message = warn_if_open_questions_missing("feat: x\n\nplain body")
        assert message is not None
        assert OPEN_QUESTIONS_HINT in message
        assert any(OPEN_QUESTIONS_HINT in record.message for record in caplog.records)

    def test_silent_when_section_present(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="teatree.core.gates.open_questions_gate"):
            message = warn_if_open_questions_missing("feat: x\n\n## Open questions & assumptions\n- none")
        assert message is None
        assert caplog.records == []


class TestCitedRatificationArtifacts:
    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("- successor choice (decided-by-user, PlanArtifact 125)", (125,)),
            ("- x (Decided By User — plan artifact #7)", (7,)),
            ("- a (decided-by-user, PlanArtifact 3)\n- b (decided-by-user, PlanArtifact 4)", (3, 4)),
            ("- a (decided-by-user, PlanArtifact 3, PlanArtifact 3)", (3,)),
            ("- an owner call with no citation (decided-by-user)", ()),
            ("- assumed: the successor is `off` — see PlanArtifact 125", ()),
            ("", ()),
        ],
    )
    def test_reads_only_citations_on_a_ratification_line(self, body: str, expected: tuple[int, ...]) -> None:
        assert cited_ratification_artifacts(body) == expected


class TestWarnIfOwnerRatificationUnbacked(TestCase):
    def _artifact(self, recorded_by: str) -> PlanArtifact:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/4371")
        return PlanArtifact.record(ticket=ticket, plan_text="the plan", recorded_by=recorded_by)

    def _warn(self, body: str) -> str | None:
        with patch.object(logging.getLogger("teatree.core.gates.open_questions_gate"), "warning") as warning:
            message = warn_if_owner_ratification_unbacked(body)
        assert (warning.call_count == 1) is (message is not None)
        return message

    def test_warns_when_the_cited_artifact_was_recorded_by_the_loop(self) -> None:
        artifact = self._artifact("loop")

        message = self._warn(f"- successor choice (decided-by-user, PlanArtifact {artifact.pk})")

        assert message is not None
        assert RATIFICATION_HINT in message
        assert f"PlanArtifact {artifact.pk} (recorded_by='loop')" in message

    def test_warns_when_the_citation_names_no_record(self) -> None:
        message = self._warn("- successor choice (decided-by-user, PlanArtifact 999999)")

        assert message is not None
        assert "PlanArtifact 999999 (no such record)" in message

    def test_silent_when_the_owner_recorded_the_cited_artifact(self) -> None:
        artifact = self._artifact("souliane")

        assert self._warn(f"- successor choice (decided-by-user, PlanArtifact {artifact.pk})") is None

    def test_silent_when_nothing_claims_owner_ratification(self) -> None:
        assert self._warn("## Open questions & assumptions\n- assumed: the successor is `off`") is None


_MOCK_OVERLAY = {"test": CommandOverlay()}


class TestShipExecutorEmitsWarn(TestCase):
    def setUp(self) -> None:
        reset_overlay_cache()
        self.addCleanup(reset_overlay_cache)

    def _ticket_with_worktree(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/1933")
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
            patch("teatree.core.runners.ship.push_branch"),
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: x", body)),
        ):
            result = ShipExecutor(ticket).run()
        assert result.ok is True
        return host

    def test_warns_when_pr_body_lacks_section(self) -> None:
        with self.assertLogs("teatree.core.gates.open_questions_gate", level="WARNING") as logs:
            host = self._run_ship("Plain body, no section.")
        host.create_pr.assert_called_once()
        assert any(OPEN_QUESTIONS_HINT in line for line in logs.output)

    def test_silent_when_pr_body_has_section(self) -> None:
        logger = logging.getLogger("teatree.core.gates.open_questions_gate")
        with patch.object(logger, "warning") as mock_warning:
            host = self._run_ship("## Open questions & assumptions\n\n- none")
        host.create_pr.assert_called_once()
        mock_warning.assert_not_called()

    def test_warns_when_a_ratification_claim_cites_no_record(self) -> None:
        body = "## Open questions & assumptions\n\n- successor choice (decided-by-user, PlanArtifact 999999)"
        with self.assertLogs("teatree.core.gates.open_questions_gate", level="WARNING") as logs:
            host = self._run_ship(body)
        host.create_pr.assert_called_once()
        assert any(RATIFICATION_HINT in line for line in logs.output)


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

    def test_warns_when_orphan_pr_body_lacks_section(self) -> None:
        with self.assertLogs("teatree.core.gates.open_questions_gate", level="WARNING") as logs:
            host = self._create_orphan_pr("plain body, no section")
        host.create_pr.assert_called_once()
        assert any(OPEN_QUESTIONS_HINT in line for line in logs.output)

    def test_silent_when_orphan_pr_body_has_section(self) -> None:
        logger = logging.getLogger("teatree.core.gates.open_questions_gate")
        with patch.object(logger, "warning") as mock_warning:
            host = self._create_orphan_pr("## Open questions & assumptions\n- none")
        host.create_pr.assert_called_once()
        mock_warning.assert_not_called()

    def test_warns_when_an_orphan_ratification_claim_cites_no_record(self) -> None:
        body = "## Open questions & assumptions\n- successor choice (decided-by-user, PlanArtifact 999999)"
        with self.assertLogs("teatree.core.gates.open_questions_gate", level="WARNING") as logs:
            host = self._create_orphan_pr(body)
        host.create_pr.assert_called_once()
        assert any(RATIFICATION_HINT in line for line in logs.output)
