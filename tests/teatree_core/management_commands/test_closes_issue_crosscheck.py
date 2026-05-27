"""Pre-push cross-check for ``Closes/Fixes/Resolves #N`` trailers (#83).

teatree's internal TaskList uses integer ids that are NOT GitHub issue
numbers, so a ``Closes #<task-id>`` trailer mis-targets an unrelated issue
that happens to carry that number. This gate cross-checks every close-keyword
reference in the MR description / commit bodies against the real issue on the
target repo: it BLOCKS when the referenced issue is closed or missing, and
WARNS (non-blocking) when the issue title shares no token with the branch
name. Scoped to overlays whose ``config.mr_close_ticket`` is set — exactly the
teatree case where ``Closes #N`` is emitted and auto-closes on merge.

Driven through the real ``pr create`` entrypoint; only the unstoppable git
subprocess + visual-QA + GitHub/GitLab network boundaries are patched.
"""

import contextlib
from collections.abc import Iterator
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings

import teatree.core.management.commands._closes_issue_crosscheck as crosscheck_mod
import teatree.core.management.commands.pr as pr_mod
from teatree.core.management.commands._closes_issue_crosscheck import (
    _issue_url_for_ref,
    _referenced_numbers,
    _shares_token,
    _tokens,
)
from teatree.core.models import Session, Ticket, Worktree
from tests.teatree_core.management_commands._overlays import (
    CLOSE_TICKET_OVERLAY,
    FULL_OVERLAY,
    SETTINGS,
    _patch_overlays,
)

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)

_ISSUE_URL = "https://github.com/souliane/teatree/issues/70"


def _shippable_ticket(
    *,
    repo: str = "/tmp/wt",
    branch: str = "83-gate-closes-issue-crosscheck",
    issue_url: str = _ISSUE_URL,
) -> Ticket:
    ticket = Ticket.objects.create(
        overlay="test",
        state=Ticket.State.REVIEWED,
        issue_url=issue_url,
    )
    session = Session.objects.create(overlay="test", ticket=ticket)
    for phase in ("testing", "reviewing", "retro"):
        session.visit_phase(phase)
    Worktree.objects.create(
        overlay="test",
        ticket=ticket,
        repo_path=repo,
        branch=branch,
        extra={"worktree_path": repo},
    )
    return ticket


@contextlib.contextmanager
def _git_boundary(
    *,
    subject: str = "feat: x",
    body: str = "",
    commit_bodies: list[str] | None = None,
) -> Iterator[None]:
    """Patch the git boundary the gate + ``ship_preview`` reach.

    Mirrors ``test_close_keyword_gate._git_boundary``: ``last_commit_message``
    is the raw MR-description source, ``commit_messages`` feeds the
    branch-commit scan, ``default_branch`` lets the range be built. Visual QA
    is patched out (browser boundary).
    """
    with (
        patch.object(pr_mod, "_run_visual_qa_gate", return_value=None),
        patch(
            "teatree.core.management.commands._closes_issue_crosscheck.git.last_commit_message",
            return_value=(subject, body),
        ),
        patch(
            "teatree.core.management.commands._pr_preview.git.last_commit_message",
            return_value=(subject, body),
        ),
        patch(
            "teatree.core.management.commands._closes_issue_crosscheck.git.default_branch",
            return_value="main",
        ),
        patch(
            "teatree.core.management.commands._closes_issue_crosscheck.git.commit_messages",
            return_value=list(commit_bodies or []),
        ),
    ):
        yield


@contextlib.contextmanager
def _stub_issue(issue: dict[str, object]) -> Iterator[None]:
    """Patch the code-host so ``get_issue`` returns *issue* (the network boundary)."""

    class _Host:
        def get_issue(self, issue_url: str) -> dict[str, object]:
            return issue

    with patch.object(crosscheck_mod, "code_host_from_overlay", return_value=_Host()):
        yield


class TestClosesIssueCrosscheckBlocks(TestCase):
    """Overlay with ``mr_close_ticket=True`` (teatree-style): the gate enforces."""

    @_patch_overlays(CLOSE_TICKET_OVERLAY)
    @override_settings(**SETTINGS)
    def test_blocks_when_referenced_issue_is_closed(self) -> None:
        ticket = _shippable_ticket()
        with (
            pytest.raises(SystemExit) as ctx,
            _git_boundary(subject="feat: gate closes-issue cross-check (#70)", body="Closes #70"),
            _stub_issue({"state": "closed", "title": "gate closes-issue cross-check"}),
        ):
            call_command("pr", "create", str(ticket.pk))
        assert ctx.value.code != 0
        assert "#70" in str(ctx.value)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED

    @_patch_overlays(CLOSE_TICKET_OVERLAY)
    @override_settings(**SETTINGS)
    def test_blocks_when_referenced_issue_is_missing(self) -> None:
        ticket = _shippable_ticket()
        with (
            pytest.raises(SystemExit) as ctx,
            _git_boundary(subject="feat: x", body="Closes #999"),
            _stub_issue({"error": "Issue not found: ...#999"}),
        ):
            call_command("pr", "create", str(ticket.pk))
        assert ctx.value.code != 0
        assert "#999" in str(ctx.value)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED

    @_patch_overlays(CLOSE_TICKET_OVERLAY)
    @override_settings(**SETTINGS)
    def test_passes_when_open_issue_title_matches_branch(self) -> None:
        ticket = _shippable_ticket()
        with (
            _git_boundary(subject="feat: gate closes-issue cross-check (#70)", body="Closes #70"),
            _stub_issue({"state": "open", "title": "Add closes-issue cross-check gate"}),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk)))
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert "error" not in result

    @_patch_overlays(CLOSE_TICKET_OVERLAY)
    @override_settings(**SETTINGS)
    def test_warns_but_proceeds_on_unrelated_open_title(self) -> None:
        ticket = _shippable_ticket(branch="83-gate-closes-issue-crosscheck")
        with (
            self.assertLogs(crosscheck_mod.logger, level="WARNING") as logs,
            _git_boundary(subject="feat: x (#70)", body="Closes #70"),
            _stub_issue({"state": "open", "title": "Refactor database connection pooling"}),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk)))
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert "error" not in result
        assert any("#70" in line for line in logs.output)


class TestClosesIssueCrosscheckScope(TestCase):
    """The gate is scoped to ``mr_close_ticket`` overlays and skips otherwise."""

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_skips_when_overlay_does_not_close_via_keyword(self) -> None:
        # FullOverlay leaves mr_close_ticket=False, so a Closes #N never
        # survives into the merged PR (sanitize_close_keywords rewrites it);
        # the cross-check gate must not even call get_issue.
        ticket = _shippable_ticket()
        called: list[str] = []

        class _Host:
            def get_issue(self, issue_url: str) -> dict[str, object]:
                called.append(issue_url)
                return {}

        with (
            _git_boundary(subject="feat: x", body="Closes #999"),
            patch.object(crosscheck_mod, "code_host_from_overlay", return_value=_Host()),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk)))
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert "error" not in result
        assert called == []

    @_patch_overlays(CLOSE_TICKET_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_close_keyword_is_a_noop(self) -> None:
        ticket = _shippable_ticket()
        called: list[str] = []

        class _Host:
            def get_issue(self, issue_url: str) -> dict[str, object]:
                called.append(issue_url)
                return {}

        with (
            _git_boundary(subject="feat: no trailer here", body="Relates to #70"),
            patch.object(crosscheck_mod, "code_host_from_overlay", return_value=_Host()),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk)))
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert "error" not in result
        assert called == []


class TestClosesIssueCrosscheckFailOpen(TestCase):
    """Unverifiable inputs (no host, no ticket repo) fail OPEN rather than block."""

    @_patch_overlays(CLOSE_TICKET_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_code_host_skips(self) -> None:
        ticket = _shippable_ticket()
        with (
            _git_boundary(subject="feat: x", body="Closes #999"),
            patch.object(crosscheck_mod, "code_host_from_overlay", return_value=None),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk)))
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert "error" not in result

    @_patch_overlays(CLOSE_TICKET_OVERLAY)
    @override_settings(**SETTINGS)
    def test_unparsable_ticket_url_skips(self) -> None:
        ticket = _shippable_ticket(issue_url="ticket-without-url")
        called: list[str] = []

        class _Host:
            def get_issue(self, issue_url: str) -> dict[str, object]:
                called.append(issue_url)
                return {}

        with (
            _git_boundary(subject="feat: x", body="Closes #999"),
            patch.object(crosscheck_mod, "code_host_from_overlay", return_value=_Host()),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk)))
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert "error" not in result
        assert called == []


class TestTokenHelpers(TestCase):
    """Pure helpers: tokenization, overlap, and issue-URL construction."""

    def test_tokens_lowercases_and_splits_on_nonalnum(self) -> None:
        assert _tokens("83-gate-closes-issue-crosscheck") == {
            "gate",
            "closes",
            "issue",
            "crosscheck",
        }

    def test_tokens_drops_short_and_numeric_tokens(self) -> None:
        # Leading issue number and 1-2 char fragments carry no intent signal.
        assert "83" not in _tokens("83-gate")
        assert _tokens("a-bb-ccc") == {"ccc"}

    def test_shares_token_true_on_overlap(self) -> None:
        assert _shares_token("Add closes-issue cross-check gate", "83-gate-closes-crosscheck")

    def test_shares_token_false_on_no_overlap(self) -> None:
        assert not _shares_token("Refactor database pooling", "83-gate-closes-crosscheck")

    def test_shares_token_true_when_branch_has_no_tokens(self) -> None:
        # A token-less branch (only the leading number) can't be cross-checked;
        # treat as related so the gate never blocks on an undecidable case.
        assert _shares_token("anything at all", "83")

    def test_issue_url_for_ref_builds_sibling_issue_url(self) -> None:
        assert (
            _issue_url_for_ref("https://github.com/souliane/teatree/issues/70", "42")
            == "https://github.com/souliane/teatree/issues/42"
        )

    def test_issue_url_for_ref_handles_gitlab_dash_issues(self) -> None:
        assert (
            _issue_url_for_ref("https://gitlab.com/grp/proj/-/issues/9", "42")
            == "https://gitlab.com/grp/proj/-/issues/42"
        )

    def test_issue_url_for_ref_returns_empty_for_unparsable(self) -> None:
        assert _issue_url_for_ref("not-a-url", "42") == ""

    def test_issue_url_for_ref_returns_empty_for_non_issue_path(self) -> None:
        # A real http URL whose path is not an ``.../issues/<n>`` form can't
        # resolve the target repo for a bare ``#N`` — fail open (empty).
        assert _issue_url_for_ref("https://github.com/souliane/teatree/pull/70", "42") == ""


class TestReferencedNumbers(TestCase):
    """``_referenced_numbers`` — bare ``#N`` extraction over the git boundary."""

    def test_no_repo_or_branch_returns_empty(self) -> None:
        worktree = Worktree(overlay="test", repo_path="", branch="")
        assert _referenced_numbers(worktree) == []

    def test_git_failure_returns_collected_sources_only(self) -> None:
        worktree = Worktree(overlay="test", repo_path="/tmp/wt", branch="feat")
        with (
            patch.object(crosscheck_mod.git, "last_commit_message", return_value=("Closes #70", "")),
            patch.object(
                crosscheck_mod.git,
                "default_branch",
                side_effect=crosscheck_mod.CommandFailedError(["git", "branch"], 128, "", "boom"),
            ),
        ):
            # The last-commit source was collected before the failure; the
            # branch-range scan is skipped (unverifiable, not a block).
            assert _referenced_numbers(worktree) == ["70"]

    def test_dedups_repeated_reference(self) -> None:
        worktree = Worktree(overlay="test", repo_path="/tmp/wt", branch="feat")
        with (
            patch.object(crosscheck_mod.git, "last_commit_message", return_value=("Closes #70", "Fixes #70")),
            patch.object(crosscheck_mod.git, "default_branch", return_value="main"),
            patch.object(crosscheck_mod.git, "commit_messages", return_value=["Resolves #70"]),
        ):
            assert _referenced_numbers(worktree) == ["70"]
