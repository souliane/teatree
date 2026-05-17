from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import pr as pr_command
from teatree.core.models import Session, Ticket, Worktree

from ._shared import _MOCK_OVERLAY, _shippable_ticket


class TestPrCreateThinWrapper(TestCase):
    """``pr create`` validates gates then triggers ``ticket.ship()`` (#140)."""

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_advances_to_shipped_when_gates_pass(self) -> None:
        ticket = _shippable_ticket()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        # Default (async) path: queued, with an explicit no-worker warning (#708).
        assert result["ticket_id"] == ticket.pk
        assert result["state"] == Ticket.State.SHIPPED
        assert result["queued"] is True
        assert "QUEUED, not performed" in result["warning"]
        assert "--sync" in result["warning"]

    def test_title_override_persisted_via_locked_merge_extra(self) -> None:
        """#800 N3: ``--title`` writes pr_title_override via locked merge.

        ``_do_ship_transition``'s ``if title:`` path now routes through
        the canonical locked ``merge_extra`` (was an unlocked
        in-``atomic`` whole-extra overwrite racing the ship worker's
        ``pr_urls``). Exercises that branch.
        """
        ticket = _shippable_ticket()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            call_command("pr", "create", str(ticket.id), title="My Custom PR Title")

        ticket.refresh_from_db()
        assert ticket.extra["pr_title_override"] == "My Custom PR Title"
        assert ticket.state == Ticket.State.SHIPPED

    def test_retrospected_with_satisfying_phases_reconciles_and_ships(self) -> None:
        """#808: RETROSPECTED with a satisfying phase ledger must ship, not deny.

        A non-terminal state with satisfying aggregated phase records must
        NOT return {'allowed': False, 'missing': []}.

        The recurring enumerated-source bug: #799 added IN_REVIEW; a ticket
        re-provisioned for a new workstream whose FSM lingered at
        RETROSPECTED was still denied with the contradictory empty-missing
        gate failure. Phase-driven reconcile makes any non-terminal state
        with satisfying records ship.
        """
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.RETROSPECTED)
        session = Session.objects.create(ticket=ticket, overlay="test")
        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.visit_phase("retro")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature-branch",
            extra={"worktree_path": "/tmp/backend"},
        )

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))

        ticket.refresh_from_db()
        # The exact recurring contradiction must NOT occur.
        assert result.get("missing") != [] or result.get("allowed") is not False, (
            f"reconcile still enumerated — denied from RETROSPECTED with empty missing: {result}"
        )
        assert ticket.state == Ticket.State.SHIPPED
        assert result["state"] == Ticket.State.SHIPPED
        assert result["queued"] is True

    def test_in_review_with_phases_split_across_three_sessions_reconciles_and_ships(self) -> None:
        """3-session-split + IN_REVIEW must ship, not deadlock (#798).

        The required chain split across 3 sessions (the maker!=checker +
        fresh-session norm) with the ticket stuck at IN_REVIEW (a failed /
        incomplete prior ship) must NOT return {'allowed': False, 'missing':
        []}. The gate aggregates phases across all sessions (so missing is
        empty); the FSM reconcile must likewise recover IN_REVIEW -> REVIEWED
        so ship() is legal and the ticket ships.
        """
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW)
        # Three distinct sessions, one phase each, distinct agent identities —
        # exactly how maker!=checker + fresh-session scheduling scatters the
        # required chain across the ticket lifecycle.
        s_test = Session.objects.create(ticket=ticket, overlay="test", agent_id="maker:a")
        s_test.visit_phase("testing", agent_id="maker:a")
        s_review = Session.objects.create(ticket=ticket, overlay="test", agent_id="reviewer:b")
        s_review.visit_phase("reviewing", agent_id="reviewer:b")
        s_retro = Session.objects.create(ticket=ticket, overlay="test", agent_id="maker:a")
        s_retro.visit_phase("retro", agent_id="maker:a")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature-branch",
            extra={"worktree_path": "/tmp/backend"},
        )

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))

        ticket.refresh_from_db()
        # Must NOT be the {'allowed': False, 'missing': []} deadlock.
        assert result.get("missing") != [] or result.get("allowed") is not False
        assert "error" not in result, result
        assert ticket.state == Ticket.State.SHIPPED
        assert result["state"] == Ticket.State.SHIPPED
        assert result["queued"] is True

    def test_returns_error_when_no_worktree(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))
        assert "error" in result

    def test_dry_run_returns_preview_without_transition(self) -> None:
        ticket = _shippable_ticket()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
            patch.object(pr_command.git, "last_commit_message", return_value=("feat: x", "body")),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id), dry_run=True))

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED  # unchanged
        assert result["dry_run"] is True
        assert result["title"] == "feat: x"
        assert result["branch"] == "feature-branch"

    def test_resolves_ticket_by_issue_url(self) -> None:
        # Calling `pr create` with the issue URL (or trailing issue number)
        # resolves to the ticket by issue_url so users don't have to look up
        # the internal DB pk first.
        ticket = _shippable_ticket()
        ticket.issue_url = "https://github.com/souliane/teatree/issues/466"
        ticket.save(update_fields=["issue_url"])

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", "https://github.com/souliane/teatree/issues/466"),
            )

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert result["ticket_id"] == ticket.pk

    def test_blocked_when_visual_qa_fails(self) -> None:
        ticket = _shippable_ticket()
        failure = pr_command.VisualQAGateFailure(
            allowed=False,
            error="Visual QA found 1 blocking finding(s).",
            visual_qa={},
            report_markdown="## Visual QA",
            hint="fix it",
        )

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=failure),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED  # not advanced
        assert result["allowed"] is False


class TestPrCreateRecordsInvokingBranch(TestCase):
    """#776 B1: ``create()`` must PRODUCE ``extra['ship_invoking_branch']``.

    The producer block (capture ``git.current_branch`` → persist the
    hint) is the single highest-value line of #776 — ShipExecutor's
    consumer side is meaningless without it. These tests exercise the
    producer directly via ``call_command``, patching only ``git`` (an
    unstoppable external). ``dry_run=True`` isolates the producer: it
    runs before the dry-run early-return and before the gates, so no
    ship mocking is needed.
    """

    def test_persists_invoking_branch_from_git_current_branch(self) -> None:
        ticket = _shippable_ticket()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="s-776-feature"),
            patch.object(pr_command.git, "last_commit_message", return_value=("feat: x", "body")),
        ):
            call_command("pr", "create", str(ticket.id), dry_run=True)

        ticket.refresh_from_db()
        assert ticket.extra["ship_invoking_branch"] == "s-776-feature"

    def test_guard_skips_persistence_on_non_feature_branch(self) -> None:
        # subTest (not @parametrize): this file's classes are
        # django.test.TestCase, which pytest does not parametrize.
        for protected in ("HEAD", "main", "master"):
            with self.subTest(branch=protected):
                ticket = _shippable_ticket()
                with (
                    patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
                    patch.object(pr_command.git, "current_branch", return_value=protected),
                    patch.object(pr_command.git, "last_commit_message", return_value=("feat: x", "body")),
                ):
                    call_command("pr", "create", str(ticket.id), dry_run=True)

                ticket.refresh_from_db()
                assert "ship_invoking_branch" not in (ticket.extra or {})

    def test_guard_skips_persistence_on_empty_branch(self) -> None:
        ticket = _shippable_ticket()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value=""),
            patch.object(pr_command.git, "last_commit_message", return_value=("feat: x", "body")),
        ):
            call_command("pr", "create", str(ticket.id), dry_run=True)

        ticket.refresh_from_db()
        assert "ship_invoking_branch" not in (ticket.extra or {})


class TestPrCreateNoCommitsAheadGuard(TestCase):
    """#788: pr create must fail loudly on a 0-commits-ahead branch.

    No hollow ``shipped`` — instead of advancing the FSM and deferring
    an empty-diff failure into the async ship worker, the branch must
    have ≥1 commit ahead of its base.
    """

    @staticmethod
    def _git(repo: str, *args: str) -> None:
        from teatree.utils import run as run_mod  # noqa: PLC0415

        run_mod.run_checked(["git", "-C", repo, *args])

    def _repo_at_parity_with_base(self, root: str) -> str:
        # A real git repo where the feature branch has NO commits ahead
        # of the base it would be compared against.
        self._git(root, "init", "-q", "-b", "main")
        self._git(root, "config", "user.email", "t@example.com")
        self._git(root, "config", "user.name", "t")
        (Path(root) / "f.txt").write_text("x", encoding="utf-8")
        self._git(root, "add", "-A")
        self._git(root, "commit", "-q", "-m", "base")
        self._git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
        self._git(root, "checkout", "-q", "-b", "feature-parity")
        return "feature-parity"

    def test_branch_at_parity_with_base_does_not_reach_shipped(self) -> None:
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as root:
            branch = self._repo_at_parity_with_base(root)
            ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
            session = Session.objects.create(ticket=ticket, overlay="test")
            session.visit_phase("testing")
            session.visit_phase("reviewing")
            session.visit_phase("retro")
            Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path=root,
                branch=branch,
                extra={"worktree_path": root},
            )

            with (
                patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
                patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
                patch.object(pr_command, "validate_pr_metadata", return_value=None),
                patch.object(pr_command.git, "current_branch", return_value=branch),
            ):
                result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))

            ticket.refresh_from_db()
            # No hollow success, no FSM transition.
            assert ticket.state == Ticket.State.REVIEWED, f"hollow shipped: state={ticket.state}"
            assert result.get("state") != Ticket.State.SHIPPED
            # Structured no-commits error naming branch + base.
            assert "error" in result
            assert branch in str(result.get("error", "")) or branch in str(result.get("branch", ""))
            assert "base" in result or "ahead" in str(result.get("error", "")).lower()
