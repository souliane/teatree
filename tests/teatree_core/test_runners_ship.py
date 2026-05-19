"""Tests for ShipExecutor — composed runner for the ship transition.

Stage 2 of #140: ``Ticket.ship()`` becomes a thin transition that enqueues
the heavy I/O (push, MR creation) onto a ``@task`` worker. The worker runs
``ShipExecutor`` and on success advances ``SHIPPED → IN_REVIEW``.
"""

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import reset_overlay_cache
from teatree.core.runners import ShipExecutor
from teatree.core.runners.ship import overlay_pr_labels, sanitize_close_keywords, should_close_ticket
from tests.teatree_core.conftest import CommandOverlay


@pytest.fixture(autouse=True)
def _clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


_MOCK_OVERLAY = {"test": CommandOverlay()}


class TestShipExecutor(TestCase):
    def _ticket_with_worktree(self, *, branch: str = "feat-x", repo: str = "/tmp/repo") -> Ticket:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/77")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=repo,
            branch=branch,
            extra={"worktree_path": repo},
        )
        return ticket

    def test_pushes_branch_then_creates_pr_and_records_url(self) -> None:
        ticket = self._ticket_with_worktree()
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://example.com/mr/1", "iid": 1}
        host.current_user.return_value = "souliane"

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push") as push,
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: x", "body")),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is True
        push.assert_called_once_with(repo="/tmp/repo", remote="origin", branch="feat-x")
        (spec,) = host.create_pr.call_args.args
        assert spec.repo == "/tmp/repo"
        assert spec.branch == "feat-x"
        assert spec.title == "feat: x"
        assert spec.assignee == "souliane"

        ticket.refresh_from_db()
        assert ticket.extra["pr_urls"] == ["https://example.com/mr/1"]

    def test_returns_failure_when_no_code_host(self) -> None:
        ticket = self._ticket_with_worktree()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_from_overlay", return_value=None),
            patch("teatree.core.runners.ship.git.push"),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is False
        assert "code host" in result.detail.lower()

    def test_returns_failure_when_no_worktree(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/78")

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = ShipExecutor(ticket).run()

        assert result.ok is False
        assert "worktree" in result.detail.lower()

    def test_description_starts_with_commit_subject(self) -> None:
        """Default MR description prepends the commit subject.

        Some overlays' CI jobs validate that the first description line matches
        the title format; previously the description was only the commit body,
        so every MR without an explicit description tripped the pipeline.
        Regression guard for overlay issue t3-o.#54 Bug 2.
        """
        ticket = self._ticket_with_worktree()
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://example.com/mr/2"}
        host.current_user.return_value = "souliane"

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push"),
            patch(
                "teatree.core.runners.ship.git.last_commit_message",
                return_value=("feat(core): add thing (https://example.com/issues/77)", "Longer body.\nMore detail."),
            ),
        ):
            ShipExecutor(ticket).run()

        (spec,) = host.create_pr.call_args.args
        assert spec.description.startswith("feat(core): add thing (https://example.com/issues/77)")
        assert spec.description == (
            "feat(core): add thing (https://example.com/issues/77)\n\nLonger body.\nMore detail."
        )

    def test_description_is_just_subject_when_body_empty(self) -> None:
        ticket = self._ticket_with_worktree()
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "u"}
        host.current_user.return_value = "dev"

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push"),
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: x", "")),
        ):
            ShipExecutor(ticket).run()

        (spec,) = host.create_pr.call_args.args
        assert spec.description == "feat: x"

    def test_assignee_falls_back_to_git_user_name_when_host_returns_empty(self) -> None:
        ticket = self._ticket_with_worktree()
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "u"}
        host.current_user.return_value = ""

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push"),
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat", "")),
            patch("teatree.core.runners.ship.git.config_value", return_value="dev"),
        ):
            ShipExecutor(ticket).run()

        (spec,) = host.create_pr.call_args.args
        assert spec.assignee == "dev"


class TestShipResolvesBranchFromInvokingWorktree(TestCase):
    """#776: ship the invoking worktree's branch, not stale first().

    A ticket spanning multiple PRs must ship the invoking worktree's
    branch, never the stale earliest ``worktrees.first()`` row.
    Reused-ticket workflows (one ticket, sequential workstreams each on
    its own branch) created a Worktree row per workstream. ``ShipExecutor``
    resolved the branch via ``ticket.worktrees.first()`` — the EARLIEST
    (already-merged) row — so ``pr create --sync`` pushed a stale merged
    branch and opened a junk duplicate PR while the intended branch was
    never pushed and ``extra['pr_urls']`` stayed empty.
    """

    def _multi_worktree_ticket(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/776")
        # PR-A: created first → the stale row worktrees.first() returns.
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/repo-pr-a",
            branch="s-776-pr-a-merged",
            extra={"worktree_path": "/tmp/repo-pr-a"},
        )
        # PR-B: the current/invoking workstream.
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/repo-pr-b",
            branch="s-776-pr-b-current",
            extra={"worktree_path": "/tmp/repo-pr-b"},
        )
        return ticket

    def test_ships_invoking_branch_not_stale_first_worktree(self) -> None:
        ticket = self._multi_worktree_ticket()
        ticket.extra = {"ship_invoking_branch": "s-776-pr-b-current"}
        ticket.save(update_fields=["extra"])
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://example.com/mr/b"}
        host.current_user.return_value = "souliane"

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push") as push,
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: b", "body")),
            patch("teatree.core.runners.ship.git.branch_merged", return_value=False),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is True
        # The intended PR-B branch is pushed — NOT the stale PR-A row.
        push.assert_called_once_with(repo="/tmp/repo-pr-b", remote="origin", branch="s-776-pr-b-current")
        (spec,) = host.create_pr.call_args.args
        assert spec.branch == "s-776-pr-b-current"
        assert spec.repo == "/tmp/repo-pr-b"
        ticket.refresh_from_db()
        assert ticket.extra["pr_urls"] == ["https://example.com/mr/b"]
        # The transient resolution hint is cleared after use.
        assert "ship_invoking_branch" not in ticket.extra

    def test_refuses_when_resolved_branch_already_merged(self) -> None:
        ticket = self._multi_worktree_ticket()
        ticket.extra = {"ship_invoking_branch": "s-776-pr-a-merged"}
        ticket.save(update_fields=["extra"])
        host = MagicMock()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push") as push,
            patch("teatree.core.runners.ship.git.branch_merged", return_value=True),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is False
        assert "merged" in result.detail.lower()
        push.assert_not_called()
        host.create_pr.assert_not_called()

    def test_falls_back_to_first_when_no_invoking_branch(self) -> None:
        """Async-worker path (no CLI cwd context): legacy behaviour kept."""
        ticket = self._multi_worktree_ticket()
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://example.com/mr/legacy"}
        host.current_user.return_value = "souliane"

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push") as push,
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat", "b")),
            patch("teatree.core.runners.ship.git.branch_merged", return_value=False),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is True
        push.assert_called_once_with(repo="/tmp/repo-pr-a", remote="origin", branch="s-776-pr-a-merged")

    def test_invoking_branch_set_but_no_matching_row_falls_back_to_first(self) -> None:
        """Recorded invoking branch has no Worktree row → legacy fallback.

        Covers the resolution arc where ``ship_invoking_branch`` is set
        but no row matches (e.g. the row was pruned); ``first()`` is used.
        """
        ticket = self._multi_worktree_ticket()
        ticket.extra = {"ship_invoking_branch": "s-776-branch-with-no-row"}
        ticket.save(update_fields=["extra"])
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://example.com/mr/fb"}
        host.current_user.return_value = "souliane"

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push") as push,
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat", "b")),
            patch("teatree.core.runners.ship.git.branch_merged", return_value=False),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is True
        push.assert_called_once_with(repo="/tmp/repo-pr-a", remote="origin", branch="s-776-pr-a-merged")

    def test_refuses_merged_branch_when_no_invoking_hint_recorded(self) -> None:
        """Merged-branch refusal with no ``ship_invoking_branch`` key set.

        Covers ``_clear_invoking_branch`` early-exit (key absent) on the
        async-worker / single-PR path where the resolved first() branch
        is already merged.
        """
        ticket = self._multi_worktree_ticket()  # no ship_invoking_branch in extra
        host = MagicMock()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push") as push,
            patch("teatree.core.runners.ship.git.branch_merged", return_value=True),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is False
        assert "merged" in result.detail.lower()
        push.assert_not_called()
        host.create_pr.assert_not_called()


class TestSanitizeCloseKeywords:
    @pytest.mark.parametrize(
        ("description", "expected"),
        [
            ("Closes #123", "Relates to #123"),
            ("Fixes #42", "Relates to #42"),
            ("Resolves #7", "Relates to #7"),
            ("closes #123", "Relates to #123"),
            ("See Closes #1 and Fixes #2", "See Relates to #1 and Relates to #2"),
            ("Closes group/project#99", "Relates to group/project#99"),
            (
                "Closes https://gitlab.com/org/project/-/issues/729",
                "Relates to https://gitlab.com/org/project/-/issues/729",
            ),
            (
                "Resolves https://github.com/owner/repo/issues/10",
                "Relates to https://github.com/owner/repo/issues/10",
            ),
            # #1090: the colon separator GitLab's default issue_closing_pattern
            # accepts ("Closes: #N" auto-closes on merge) must be rewritten too.
            ("Closes: #1", "Relates to #1"),
            ("closes:#1", "Relates to #1"),
            ("Fixes:  #1", "Relates to #1"),
            ("Closes: group/project#99", "Relates to group/project#99"),
            (
                "Resolves: https://gitlab.com/org/project/-/issues/729",
                "Relates to https://gitlab.com/org/project/-/issues/729",
            ),
            # #1090: past-tense verbs (the gate already rejected these; the
            # sanitizer must match the unified superset so the two stay in lockstep).
            ("Closed #5", "Relates to #5"),
            ("Fixed: #6", "Relates to #6"),
            ("No ticket ref here", "No ticket ref here"),
            ("", ""),
            # Negatives — must stay unchanged (no new false positives).
            ("Relates to #1", "Relates to #1"),
            ("Refs #1", "Refs #1"),
            ("See #1", "See #1"),
            # The \b word boundary keeps "discloses" from matching "closes".
            ("This discloses #1 in a sentence", "This discloses #1 in a sentence"),
            # Space BEFORE the colon is intentionally NOT matched: GitLab's real
            # issue_closing_pattern is `(:?) +`, so `Closes : #1` does not auto-close.
            ("Closes : #1", "Closes : #1"),
        ],
    )
    def test_replaces_close_keywords_when_close_ticket_false(self, description: str, expected: str) -> None:
        assert sanitize_close_keywords(description, close_ticket=False) == expected

    def test_leaves_description_unchanged_when_close_ticket_true(self) -> None:
        assert sanitize_close_keywords("Closes #123", close_ticket=True) == "Closes #123"


class TestShouldCloseTicket:
    """The auto-close disposition resolver (#873).

    Default = close-on-merge when the overlay setting is enabled.
    Suppression is the exception: only an explicit ``more_prs_coming``
    opt-out (declared partial / umbrella with remaining scope) keeps the
    issue open.
    """

    def test_setting_enabled_standalone_full_resolve_closes(self) -> None:
        # (a) setting True + standalone non-umbrella full-resolve PR ⇒ close.
        assert should_close_ticket({}, setting_enabled=True) is True

    def test_setting_enabled_none_extra_closes(self) -> None:
        # Orphan-branch path with no ticket extra still close-on-merge.
        assert should_close_ticket(None, setting_enabled=True) is True

    def test_setting_enabled_explicit_followup_opt_out_keeps_open(self) -> None:
        # (b) umbrella / declared-partial PR ⇒ issue stays open.
        assert should_close_ticket({"more_prs_coming": True}, setting_enabled=True) is False

    def test_setting_disabled_never_closes(self) -> None:
        # (c) setting False ⇒ no auto-close regardless of opt-out flag.
        assert should_close_ticket({}, setting_enabled=False) is False
        assert should_close_ticket({"more_prs_coming": True}, setting_enabled=False) is False

    def test_followup_flag_falsey_value_still_closes(self) -> None:
        # An explicitly-false / absent opt-out keeps the close-on-merge default.
        assert should_close_ticket({"more_prs_coming": False}, setting_enabled=True) is True


class TestShipExecutorHonorsAutoCloseSetting(TestCase):
    """End-to-end: the PR description the ship path sends to the code host.

    Proves the setting is wired through ``_build_pr_spec`` — the exact
    regression: a True setting + standalone PR must keep ``Closes #N`` so
    the platform auto-closes on merge; an explicit ``more_prs_coming``
    opt-out (umbrella/partial) must rewrite to ``Relates to``.
    """

    def _ticket_with_extra(self, extra: dict) -> Ticket:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://github.com/souliane/teatree/issues/873",
            extra=extra,
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/repo873",
            branch="fix/873",
            extra={"worktree_path": "/tmp/repo873"},
        )
        return ticket

    def _capture_pr_description(self, ticket: Ticket, *, setting_enabled: bool) -> str:
        host = MagicMock()
        host.create_pr.return_value = {"html_url": "https://github.com/souliane/teatree/pull/1"}
        host.current_user.return_value = "souliane"
        cfg = MagicMock()
        cfg.config.mr_close_ticket = setting_enabled
        cfg.config.pr_auto_labels = []
        with (
            patch("teatree.core.runners.ship.code_host_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.get_overlay", return_value=cfg),
            patch("teatree.core.runners.ship.overlay_pr_labels", return_value=[]),
            patch("teatree.core.runners.ship.git.branch_merged", return_value=False),
            patch("teatree.core.runners.ship.git.push"),
            patch(
                "teatree.core.runners.ship.git.last_commit_message",
                return_value=("fix(ship): honor auto-close", "Closes #873"),
            ),
            patch("teatree.core.runners.ship.git.config_value", return_value="souliane"),
        ):
            ShipExecutor(ticket).run()
        return host.create_pr.call_args[0][0].description

    def test_setting_true_standalone_keeps_closes_keyword(self) -> None:
        ticket = self._ticket_with_extra({})
        description = self._capture_pr_description(ticket, setting_enabled=True)
        assert "Closes #873" in description
        assert "Relates to #873" not in description

    def test_setting_true_umbrella_partial_rewrites_to_relates(self) -> None:
        ticket = self._ticket_with_extra({"more_prs_coming": True})
        description = self._capture_pr_description(ticket, setting_enabled=True)
        assert "Relates to #873" in description
        assert "Closes #873" not in description

    def test_setting_false_rewrites_to_relates(self) -> None:
        ticket = self._ticket_with_extra({})
        description = self._capture_pr_description(ticket, setting_enabled=False)
        assert "Relates to #873" in description
        assert "Closes #873" not in description


class TestOverlayPrLabels:
    def test_default_overlay_returns_empty(self) -> None:
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            assert overlay_pr_labels() == []

    def test_overlay_with_string_labels(self) -> None:
        mock = MagicMock()
        mock.config.pr_auto_labels = "label-a, label-b"
        with patch("teatree.core.overlay_loader._discover_overlays", return_value={"test": mock}):
            assert overlay_pr_labels() == ["label-a", "label-b"]

    def test_non_iterable_returns_empty(self) -> None:
        mock = MagicMock()
        mock.config.pr_auto_labels = 42
        with patch("teatree.core.overlay_loader._discover_overlays", return_value={"test": mock}):
            assert overlay_pr_labels() == []
