"""Tests for ShipExecutor — composed runner for the ship transition.

Stage 2 of #140: ``Ticket.ship()`` becomes a thin transition that enqueues
the heavy I/O (push, MR creation) onto a ``@task`` worker. The worker runs
``ShipExecutor`` and on success advances ``SHIPPED → IN_REVIEW``.
"""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.backend_protocols import BackendResolutionError, PrOpenState
from teatree.core.gates import debt_delta_gate, pr_budget_gate
from teatree.core.models import PullRequest, Ticket, Worktree
from teatree.core.runners import ShipExecutor
from teatree.core.runners.base import RunnerResult
from teatree.core.runners.ship import overlay_pr_labels, sanitize_close_keywords, should_close_ticket
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}

_GIT = shutil.which("git") or "git"


def _run_git(*args: str, cwd: Path) -> None:
    subprocess.run([_GIT, "-C", str(cwd), *args], check=True, capture_output=True)


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
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
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

    def test_loop_ship_path_refuses_second_pr_at_repo_budget(self) -> None:
        # North-star PR-2: the autonomous loop's task-driven ship reaches
        # host.create_pr through ShipExecutor.run WITHOUT _run_ship_gates, so the
        # budget gate must live at the ShipExecutor chokepoint. With the cap at 1
        # and one open PR already recorded for this (repo, ticket), the ship is
        # refused and NO PR is created. RED before the _open_pr_and_record guard:
        # host.create_pr is called on the pre-fix code.
        slug = "souliane/teatree"
        ticket = self._ticket_with_worktree()
        PullRequest.objects.create(
            ticket=ticket,
            url=f"https://github.com/{slug}/pull/1",
            repo=slug,
            iid="1",
            overlay="test",
        )
        host = MagicMock()
        host.create_pr.return_value = {"web_url": f"https://github.com/{slug}/pull/2"}
        host.current_user.return_value = "souliane"

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push"),
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: x", "body")),
            patch("teatree.core.runners.ship.git.remote_slug", return_value=slug),
            patch.object(
                pr_budget_gate,
                "get_effective_settings",
                return_value=UserSettings(max_open_prs_per_repo_per_ticket=1),
            ),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is False
        assert "max_open_prs_per_repo_per_ticket" in result.detail
        host.create_pr.assert_not_called()

    def test_loop_ship_path_allows_pr_when_budget_not_reached(self) -> None:
        # Inert-at-limit companion: with the cap at 1 and no existing open PR for
        # this (repo, ticket), the loop ship proceeds and opens the PR.
        slug = "souliane/teatree"
        ticket = self._ticket_with_worktree()
        host = MagicMock()
        host.create_pr.return_value = {"web_url": f"https://github.com/{slug}/pull/1"}
        host.current_user.return_value = "souliane"

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push"),
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: x", "body")),
            patch("teatree.core.runners.ship.git.remote_slug", return_value=slug),
            patch.object(
                pr_budget_gate,
                "get_effective_settings",
                return_value=UserSettings(max_open_prs_per_repo_per_ticket=1),
            ),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is True
        host.create_pr.assert_called_once()

    def test_loop_ship_path_refuses_net_new_debt(self) -> None:
        # North-star PR-3: the autonomous loop's task-driven ship reaches
        # host.create_pr through ShipExecutor.run WITHOUT _run_ship_gates — the
        # same bypass class the budget gate closed. With require_debt_delta on and
        # a net-new noqa in the branch diff, the ship is refused and NO PR is
        # created. RED before the _open_pr_and_record debt guard: the pre-fix loop
        # path calls host.create_pr with the debt un-gated.
        slug = "souliane/teatree"
        ticket = self._ticket_with_worktree()
        host = MagicMock()
        host.create_pr.return_value = {"web_url": f"https://github.com/{slug}/pull/1"}
        host.current_user.return_value = "souliane"
        new_noqa = (
            "diff --git a/src/teatree/m.py b/src/teatree/m.py\n"
            "--- a/src/teatree/m.py\n+++ b/src/teatree/m.py\n@@ -1,1 +1,2 @@\n"
            " keep = 1\n+risky = frobnicate()  # noqa: F821\n"
        )

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push"),
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: x", "body")),
            patch("teatree.core.runners.ship.git.remote_slug", return_value=slug),
            patch.object(debt_delta_gate, "get_effective_settings", return_value=UserSettings(require_debt_delta=True)),
            patch.object(debt_delta_gate.git, "branch_diff", return_value=new_noqa),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is False
        assert "debt_delta_gate" in result.detail
        host.create_pr.assert_not_called()

    def test_loop_ship_path_allows_pr_when_diff_is_clean(self) -> None:
        # Inert companion: require_debt_delta on but the branch introduces no
        # net-new debt, so the loop ship proceeds and opens the PR.
        slug = "souliane/teatree"
        ticket = self._ticket_with_worktree()
        host = MagicMock()
        host.create_pr.return_value = {"web_url": f"https://github.com/{slug}/pull/1"}
        host.current_user.return_value = "souliane"
        clean = (
            "diff --git a/src/teatree/m.py b/src/teatree/m.py\n"
            "--- a/src/teatree/m.py\n+++ b/src/teatree/m.py\n@@ -1,1 +1,2 @@\n"
            " keep = 1\n+clean = compute()\n"
        )

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push"),
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: x", "body")),
            patch("teatree.core.runners.ship.git.remote_slug", return_value=slug),
            patch.object(debt_delta_gate, "get_effective_settings", return_value=UserSettings(require_debt_delta=True)),
            patch.object(debt_delta_gate.git, "branch_diff", return_value=clean),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is True
        host.create_pr.assert_called_once()

    def test_returns_failure_when_no_code_host(self) -> None:
        ticket = self._ticket_with_worktree()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=None),
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

    def test_returns_failure_when_backend_returns_empty_url(self) -> None:
        """#1226 / #1222: empty backend URL must surface as ``ok=False``.

        The producer (``host.create_pr``) is expected to refuse empty URLs
        (covered in the GitHub backend tests). This consumer-side guard is
        belt-and-braces: even if a backend mis-returns ``{}`` or a dict with
        only ``url=""`` / ``web_url=""``, the ship runner must NOT advance
        the FSM to ``SHIPPED`` with an empty ``pr_urls`` entry.
        """
        ticket = self._ticket_with_worktree()
        host = MagicMock()
        host.create_pr.return_value = {"web_url": ""}
        host.current_user.return_value = "souliane"

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push"),
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: x", "body")),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is False
        assert "url" in result.detail.lower()
        ticket.refresh_from_db()
        assert "pr_urls" not in (ticket.extra or {})

    def test_create_pr_url_that_fails_reread_reports_failure_with_no_url_recorded(self) -> None:
        """#1194 verify-by-re-read: a create URL whose re-read 404s is not trusted.

        ``create_pr`` handed back a well-formed URL for the right repo, but a fresh
        independent GET reports ``UNKNOWN`` (the create silently no-op'd / the PR
        does not exist). The ship runner MUST report failure and record no
        ``pr_urls`` entry — no phantom PR advances the FSM.
        """
        ticket = self._ticket_with_worktree()
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://example.com/mr/phantom"}
        host.current_user.return_value = "souliane"
        host.get_pr_open_state.return_value = PrOpenState.UNKNOWN

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push"),
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: x", "body")),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is False
        assert "verify-by-re-read" in result.detail
        ticket.refresh_from_db()
        assert "pr_urls" not in (ticket.extra or {})

    def test_accepts_html_url_for_github_native_payloads(self) -> None:
        """The consumer reads ``html_url`` too — GitHub's native API key.

        The canonical cross-host key is ``web_url`` (GitLab) and the GitHub
        backend produces it. But raw GitHub API payloads piped through other
        producers (e.g. webhooks, lists) carry ``html_url`` natively. The
        consumer's fallback chain must keep accepting it so future code that
        forwards raw payloads doesn't hit the same field-name silent-failure
        trap as #1222.
        """
        ticket = self._ticket_with_worktree()
        host = MagicMock()
        host.create_pr.return_value = {"html_url": "https://github.com/org/repo/pull/9"}
        host.current_user.return_value = "souliane"

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push"),
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: x", "body")),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is True
        assert result.detail == "https://github.com/org/repo/pull/9"
        ticket.refresh_from_db()
        assert ticket.extra["pr_urls"] == ["https://github.com/org/repo/pull/9"]

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
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push"),
            patch(
                "teatree.core.runners.ship.git.last_commit_message",
                return_value=("feat(core): add thing (https://example.com/issues/77)", "Longer body.\nMore detail."),
            ),
        ):
            ShipExecutor(ticket).run()

        (spec,) = host.create_pr.call_args.args
        assert spec.description.startswith("feat(core): add thing (https://example.com/issues/77)")
        # #312: the generator emits the standard What/Why body by default when
        # the commit body omits it, so a thin commit still ships a scaffold.
        assert spec.description == (
            "feat(core): add thing (https://example.com/issues/77)\n\nLonger body.\nMore detail.\n\n## What\n\n## Why"
        )

    def test_description_is_just_subject_when_body_empty(self) -> None:
        ticket = self._ticket_with_worktree()
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://example.com/mr/u"}
        host.current_user.return_value = "dev"

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push"),
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: x", "")),
        ):
            ShipExecutor(ticket).run()

        (spec,) = host.create_pr.call_args.args
        # #312: an empty commit body still gets the standard What/Why scaffold.
        assert spec.description == "feat: x\n\n## What\n\n## Why"

    def test_assignee_empty_when_host_login_empty_and_no_registry_identity(self) -> None:
        # #3100: git user.name is a display name, not a forge login, so it is not
        # an assignee candidate. With an empty host login and no trusted-identity
        # registry handle, the PR is created UNASSIGNED rather than with a login
        # the forge would reject (which would fail the whole create).
        ticket = self._ticket_with_worktree()
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://example.com/mr/u"}
        host.current_user.return_value = ""

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push"),
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat", "")),
        ):
            ShipExecutor(ticket).run()

        (spec,) = host.create_pr.call_args.args
        assert spec.assignee == ""


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
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
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
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
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
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
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
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
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
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push") as push,
            patch("teatree.core.runners.ship.git.branch_merged", return_value=True),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is False
        assert "merged" in result.detail.lower()
        push.assert_not_called()
        host.create_pr.assert_not_called()


class TestShipMultiWorkstreamStaleUrlGuard(TestCase):
    """#1263: ``ShipExecutor.run`` must not short-circuit on a stale prior URL.

    Reused-ticket / multi-workstream flow: one ticket spans several PRs,
    each on its own branch. The first workstream records its URL on
    ``extra['pr_urls']``; a second workstream then invokes ship on a
    different branch. The legacy short-circuit returned the first
    workstream's URL on truthiness alone — the new branch was never
    pushed and no PR was opened, yet ``pr create --sync`` reported
    success and the FSM advanced to ``IN_REVIEW``.

    The guard: when ``ship_invoking_branch`` names a branch whose URL is
    not the one recorded for that branch, the runner must proceed to
    push and open a new PR for the invoking branch.
    """

    def _multi_worktree_ticket(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/1263")
        # Workstream A (already shipped; URL on the ticket).
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/repo-1263-a",
            branch="s-1263-pr-a-shipped",
            extra={"worktree_path": "/tmp/repo-1263-a"},
        )
        # Workstream B (current; needs its own PR).
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/repo-1263-b",
            branch="s-1263-pr-b-current",
            extra={"worktree_path": "/tmp/repo-1263-b"},
        )
        return ticket

    def test_does_not_short_circuit_when_prior_url_is_for_a_different_branch(self) -> None:
        """Stale ``pr_urls`` from workstream A must not skip workstream B."""
        ticket = self._multi_worktree_ticket()
        ticket.extra = {
            "pr_urls": ["https://example.com/pr/a-shipped"],
            "ship_invoking_branch": "s-1263-pr-b-current",
        }
        ticket.save(update_fields=["extra"])
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://example.com/pr/b-new"}
        host.current_user.return_value = "souliane"

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push") as push,
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: b", "body")),
            patch("teatree.core.runners.ship.git.branch_merged", return_value=False),
        ):
            result = ShipExecutor(ticket).run()

        # The runner must push the current invoking branch and open a new PR —
        # NOT silently return the stale workstream-A URL.
        assert result.ok is True
        assert result.detail == "https://example.com/pr/b-new"
        push.assert_called_once_with(repo="/tmp/repo-1263-b", remote="origin", branch="s-1263-pr-b-current")
        host.create_pr.assert_called_once()
        (spec,) = host.create_pr.call_args.args
        assert spec.branch == "s-1263-pr-b-current"
        ticket.refresh_from_db()
        # Both URLs are recorded; the new one is appended (not replacing the prior).
        assert "https://example.com/pr/a-shipped" in ticket.extra["pr_urls"]
        assert "https://example.com/pr/b-new" in ticket.extra["pr_urls"]

    def test_short_circuits_when_current_branch_already_has_pr_recorded(self) -> None:
        """Idempotent retry: same branch + URL already mapped ⇒ short-circuit.

        The guard rail must NOT regress the original idempotency: a retry
        of ship on the SAME workstream (the recorded URL is for this
        branch) must still short-circuit and return the recorded URL.
        """
        ticket = self._multi_worktree_ticket()
        ticket.extra = {
            "pr_urls": ["https://example.com/pr/b-already"],
            "pr_url_by_branch": {"s-1263-pr-b-current": "https://example.com/pr/b-already"},
            "ship_invoking_branch": "s-1263-pr-b-current",
        }
        ticket.save(update_fields=["extra"])
        host = MagicMock()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push") as push,
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is True
        assert result.detail == "https://example.com/pr/b-already"
        push.assert_not_called()
        host.create_pr.assert_not_called()

    def test_records_pr_url_by_branch_for_each_workstream(self) -> None:
        """After a successful ship, the URL is recorded against its branch."""
        ticket = self._multi_worktree_ticket()
        ticket.extra = {"ship_invoking_branch": "s-1263-pr-b-current"}
        ticket.save(update_fields=["extra"])
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://example.com/pr/b-new"}
        host.current_user.return_value = "souliane"

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push"),
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: b", "body")),
            patch("teatree.core.runners.ship.git.branch_merged", return_value=False),
        ):
            ShipExecutor(ticket).run()

        ticket.refresh_from_db()
        assert ticket.extra["pr_url_by_branch"]["s-1263-pr-b-current"] == "https://example.com/pr/b-new"


class TestShipReconcilesWorktreeBranch(TestCase):
    """#1519: ship pushes the worktree's ACTUAL git branch and reconciles the DB.

    ``workspace ticket <N>`` mints ``Worktree.branch`` as ``<N>-ticket``;
    the agent renames the git branch in the worktree to the
    ``<N>-<type>-<desc>`` convention. ``ShipExecutor`` pushed the
    DB-recorded (stale) ref and left the worktree↔branch DB mapping
    desynced. The fix resolves the current git branch, pushes that, and
    reconciles ``Worktree.branch`` (and ``Ticket.extra['branch']``) — but
    only for a real branch that belongs to this ticket; a detached HEAD or
    an unrelated branch falls back to the recorded branch.
    """

    @pytest.fixture(autouse=True)
    def _tmp_repo(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.repo)
        _run_git("config", "user.email", "t@t", cwd=self.repo)
        _run_git("config", "user.name", "t", cwd=self.repo)
        _run_git("commit", "--allow-empty", "-q", "-m", "initial", cwd=self.repo)

    def _checkout(self, branch: str) -> None:
        (self.repo / "f.txt").write_text(branch, encoding="utf-8")
        _run_git("checkout", "-q", "-b", branch, cwd=self.repo)
        _run_git("add", "f.txt", cwd=self.repo)
        _run_git("commit", "-q", "-m", f"work on {branch}", cwd=self.repo)

    def _ticket(self, *, recorded_branch: str, extra: dict | None = None) -> Ticket:
        merged = {"branch": recorded_branch, **(extra or {})}
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://github.com/souliane/teatree/issues/1519",
            extra=merged,
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=str(self.repo),
            branch=recorded_branch,
            extra={"worktree_path": str(self.repo)},
        )
        return ticket

    def _host(self) -> MagicMock:
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://example.com/pr/1519"}
        host.current_user.return_value = "souliane"
        return host

    def test_pushes_actual_branch_and_reconciles_db_on_drift(self) -> None:
        self._checkout("1519-fix-foo")
        ticket = self._ticket(recorded_branch="1519-ticket")
        host = self._host()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push") as push,
            patch("teatree.core.runners.ship.git.branch_merged", return_value=False),
            patch("teatree.core.runners.ship.sha_conflicts_with_target", return_value=None),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is True
        # (a) pushes the REAL branch, not the stale recorded one.
        push.assert_called_once_with(repo=str(self.repo), remote="origin", branch="1519-fix-foo")
        (spec,) = host.create_pr.call_args.args
        assert spec.branch == "1519-fix-foo"
        # (b) the DB rows are reconciled to the current branch.
        ticket.refresh_from_db()
        assert ticket.worktrees.get().branch == "1519-fix-foo"
        assert ticket.extra["branch"] == "1519-fix-foo"

    def test_no_drift_leaves_db_unchanged(self) -> None:
        self._checkout("1519-fix-foo")
        ticket = self._ticket(recorded_branch="1519-fix-foo")
        host = self._host()
        worktree = ticket.worktrees.get()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push") as push,
            patch("teatree.core.runners.ship.git.branch_merged", return_value=False),
            patch("teatree.core.runners.ship.sha_conflicts_with_target", return_value=None),
            patch.object(Worktree, "save", autospec=True) as wt_save,
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is True
        push.assert_called_once_with(repo=str(self.repo), remote="origin", branch="1519-fix-foo")
        # No spurious reconcile write when the names already agree.
        wt_save.assert_not_called()
        worktree.refresh_from_db()
        assert worktree.branch == "1519-fix-foo"

    def test_detached_head_falls_back_to_recorded_branch(self) -> None:
        self._checkout("1519-fix-foo")
        sha = subprocess.run(
            [_GIT, "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        _run_git("checkout", "-q", sha, cwd=self.repo)  # detached HEAD
        ticket = self._ticket(recorded_branch="1519-ticket")
        host = self._host()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push") as push,
            patch("teatree.core.runners.ship.git.branch_merged", return_value=False),
            patch("teatree.core.runners.ship.sha_conflicts_with_target", return_value=None),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is True
        # Falls back to the recorded branch — never pushes the bare SHA / HEAD.
        push.assert_called_once_with(repo=str(self.repo), remote="origin", branch="1519-ticket")
        ticket.refresh_from_db()
        assert ticket.worktrees.get().branch == "1519-ticket"

    def test_unrelated_branch_falls_back_and_is_not_pushed(self) -> None:
        self._checkout("9999-someone-elses-branch")  # not prefixed 1519-
        ticket = self._ticket(recorded_branch="1519-ticket")
        host = self._host()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push") as push,
            patch("teatree.core.runners.ship.git.branch_merged", return_value=False),
            patch("teatree.core.runners.ship.sha_conflicts_with_target", return_value=None),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is True
        push.assert_called_once_with(repo=str(self.repo), remote="origin", branch="1519-ticket")
        (spec,) = host.create_pr.call_args.args
        assert spec.branch == "1519-ticket"
        ticket.refresh_from_db()
        assert ticket.worktrees.get().branch == "1519-ticket"

    def test_redelivery_adopts_recorded_url_after_reconcile(self) -> None:
        """#1522 idempotency holds: a second run adopts the recorded PR url.

        The first run reconciles the branch and records the url under the
        ACTUAL branch; the redelivered job resolves the same branch and
        short-circuits on the recorded url instead of re-creating the PR.
        """
        self._checkout("1519-fix-foo")
        ticket = self._ticket(recorded_branch="1519-ticket")
        host = self._host()

        patches = (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push"),
            patch("teatree.core.runners.ship.git.branch_merged", return_value=False),
            patch("teatree.core.runners.ship.sha_conflicts_with_target", return_value=None),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            first = ShipExecutor(ticket).run()
        assert first.ok is True
        assert host.create_pr.call_count == 1

        ticket.refresh_from_db()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            second = ShipExecutor(ticket).run()

        assert second.ok is True
        assert second.detail == "https://example.com/pr/1519"
        # No second PR — the recorded url for the reconciled branch is adopted.
        assert host.create_pr.call_count == 1


class TestShipResolvesBackendFromRepoHost(TestCase):
    """#2025: ship resolves the forge from the repo's origin host.

    The ship path resolved the backend via token-presence precedence
    (GitHub first when both PATs are set), so a GitLab-hosted repo on an
    overlay carrying both PATs ran ``gh pr create`` against a GitLab
    remote and failed with ``Could not resolve to a Repository``. The
    backend must derive from where the repo actually lives.
    """

    @pytest.fixture(autouse=True)
    def _gitlab_repo(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "gl-repo"
        self.repo.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.repo)
        _run_git("config", "user.email", "t@t", cwd=self.repo)
        _run_git("config", "user.name", "t", cwd=self.repo)
        _run_git("remote", "add", "origin", "git@gitlab.com:group/repo.git", cwd=self.repo)
        _run_git("commit", "--allow-empty", "-q", "-m", "feat: x", cwd=self.repo)
        _run_git("checkout", "-q", "-b", "547-fix-foo", cwd=self.repo)
        _run_git("commit", "--allow-empty", "-q", "-m", "feat: x", cwd=self.repo)

    def _ticket(self) -> Ticket:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/group/repo/-/issues/2025",
            extra={"branch": "547-fix-foo"},
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=str(self.repo),
            branch="547-fix-foo",
            extra={"worktree_path": str(self.repo)},
        )
        return ticket

    def test_gitlab_repo_creates_pr_via_gitlab_backend(self) -> None:
        ticket = self._ticket()
        overlay = MagicMock()
        overlay.config.get_github_token.return_value = "gh-tok"
        overlay.config.get_gitlab_token.return_value = "gl-tok"
        overlay.config.gitlab_url = "https://gitlab.com"
        overlay.config.code_host = ""

        calls: list[str] = []

        def record(name: str, url: str):
            def _create_pr(self: object, spec: object) -> dict[str, str]:
                calls.append(name)
                return {"web_url": url}

            return _create_pr

        with (
            patch("teatree.core.backend_factory.get_overlay", return_value=overlay),
            patch(
                "teatree.backends.gitlab.client.GitLabCodeHost.create_pr",
                autospec=True,
                side_effect=record("gitlab", "https://gitlab.com/group/repo/-/merge_requests/1"),
            ),
            patch(
                "teatree.backends.github.client.GitHubCodeHost.create_pr",
                autospec=True,
                side_effect=record("github", "https://github.com/group/repo/pull/1"),
            ),
            patch("teatree.backends.gitlab.client.GitLabCodeHost.current_user", autospec=True, return_value="souliane"),
            patch("teatree.backends.github.client.GitHubCodeHost.current_user", autospec=True, return_value="souliane"),
            # #1194: the create is verify-by-re-read confirmed against a live GET.
            patch(
                "teatree.backends.gitlab.client.GitLabCodeHost.get_pr_open_state",
                autospec=True,
                return_value=PrOpenState.OPEN,
            ),
            patch(
                "teatree.backends.github.client.GitHubCodeHost.get_pr_open_state",
                autospec=True,
                return_value=PrOpenState.OPEN,
            ),
            patch("teatree.core.runners.ship.git.push"),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is True, result.detail
        # The PR is created via the GitLab backend — NOT GitHub (the bug:
        # token precedence picked GitHub and ran gh against a GitLab remote).
        assert calls == ["gitlab"]

    def test_resolution_error_returns_structured_failure_before_pr_attempt(self) -> None:
        """The central AC: a mismatched forge surfaces as a structured failure.

        ``BackendResolutionError`` from the resolver must become a clean
        ``RunnerResult(ok=False)`` — never an unhandled exception — and no
        push or PR-create is attempted.
        """
        ticket = self._ticket()

        with (
            patch(
                "teatree.core.runners.ship.code_host_for_repo_from_overlay",
                side_effect=BackendResolutionError("repo origin resolves to the gitlab forge but no gitlab token"),
            ),
            patch("teatree.core.runners.ship.git.push") as push,
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is False
        assert "gitlab" in result.detail
        push.assert_not_called()


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
        # The default title hook returns the subject unchanged; mirror that so
        # this auto-close test exercises sanitization, not title generation.
        cfg.metadata.build_pr_title.side_effect = lambda *, branch, subject, body, issue_url: subject
        with (
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
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


class TestShipExecutorHonorsTitleOverride(TestCase):
    """The ship path PRODUCES the title via the shared ``resolve_pr_title``.

    A title pinned on ``extra['pr_title_override']`` is what ships — the same
    title ``ship_preview`` previews and the preflight validates. This is the
    ship-side half of the title-resolution parity; ``test_pr_preview`` covers
    the preview/preflight side.
    """

    def _ticket_with_extra(self, extra: dict) -> Ticket:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://github.com/souliane/teatree/issues/298",
            extra=extra,
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/repo298",
            branch="298-fix-thing",
            extra={"worktree_path": "/tmp/repo298"},
        )
        return ticket

    def _capture_pr_title(self, ticket: Ticket) -> str:
        host = MagicMock()
        host.create_pr.return_value = {"html_url": "https://github.com/souliane/teatree/pull/1"}
        host.current_user.return_value = "souliane"
        cfg = MagicMock()
        cfg.config.mr_close_ticket = True
        cfg.config.pr_auto_labels = []
        cfg.metadata.build_pr_title.side_effect = lambda *, branch, subject, body, issue_url: subject
        with (
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.get_overlay", return_value=cfg),
            patch("teatree.core.runners.ship.overlay_pr_labels", return_value=[]),
            patch("teatree.core.runners.ship.git.branch_merged", return_value=False),
            patch("teatree.core.runners.ship.git.push"),
            patch(
                "teatree.core.runners.ship.git.last_commit_message",
                return_value=("chore: unrelated subject (#298)", "Body."),
            ),
            patch("teatree.core.runners.ship.git.config_value", return_value="souliane"),
        ):
            ShipExecutor(ticket).run()
        return host.create_pr.call_args[0][0].title

    def test_pr_title_override_is_the_shipped_title(self) -> None:
        ticket = self._ticket_with_extra({"pr_title_override": "fix(scope): pinned title (#298)"})
        title = self._capture_pr_title(ticket)
        assert title == "fix(scope): pinned title (#298)"

    def test_no_override_falls_back_to_subject(self) -> None:
        ticket = self._ticket_with_extra({})
        title = self._capture_pr_title(ticket)
        assert title == "chore: unrelated subject (#298)"


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


class TestShipPrUrlRepoMismatch(TestCase):
    """#1120 (a): the PR URL must point at the expected repo.

    ``host.create_pr`` returning a syntactically-valid URL for the *wrong*
    repo (e.g. a cross-project CI mirror) must surface as ``ok=False`` and
    must NOT advance the FSM to ``in_review`` or record a ``pr_urls`` entry.
    """

    _EXPECTED_SLUG = "expected-org/expected-repo"
    _EXPECTED_URL = "https://github.com/expected-org/expected-repo/pull/42"
    _WRONG_URL = "https://github.com/other-org/other-repo/pull/1"

    def _ticket_with_worktree(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/99")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/expected-repo",
            branch="feat-y",
            extra={"worktree_path": "/tmp/expected-repo"},
        )
        return ticket

    def _run_ship(self, ticket: Ticket, pr_url: str) -> RunnerResult:
        host = MagicMock()
        host.create_pr.return_value = {"web_url": pr_url}
        host.current_user.return_value = "souliane"

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push"),
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: y", "body")),
            patch("teatree.core.runners.ship.git.remote_slug", return_value=self._EXPECTED_SLUG),
        ):
            return ShipExecutor(ticket).run()

    def test_returns_failure_when_pr_url_targets_wrong_repo(self) -> None:
        """PR URL for a different repo → ``ok=False``, FSM does not advance."""
        ticket = self._ticket_with_worktree()

        result = self._run_ship(ticket, self._WRONG_URL)

        assert result.ok is False
        assert self._EXPECTED_SLUG in result.detail
        ticket.refresh_from_db()
        assert "pr_urls" not in (ticket.extra or {})

    def test_returns_success_when_pr_url_matches_expected_repo(self) -> None:
        """PR URL containing the expected slug → ``ok=True``, URL recorded."""
        ticket = self._ticket_with_worktree()

        result = self._run_ship(ticket, self._EXPECTED_URL)

        assert result.ok is True
        assert result.detail == self._EXPECTED_URL
        ticket.refresh_from_db()
        assert ticket.extra["pr_urls"] == [self._EXPECTED_URL]
