"""Pre-push close-keyword gate (#1012).

The gate is overlay-scoped: it rejects ``Closes/Fixes/Resolves #N`` (and the
full GitHub/GitLab auto-close keyword set, case-insensitive, ``#N`` and
full-URL forms) in the MR description or any branch commit body — but ONLY
for overlays that set ``config.forbid_close_keywords``; teatree's own
overlay does not. Driven through the real ``pr create`` entrypoint;
only the unstoppable git subprocess + visual-QA boundary are patched.
"""

import contextlib
from collections.abc import Iterator
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings

import teatree.core.management.commands._close_keyword_gate as gate_mod
import teatree.core.management.commands.pr as pr_mod
from teatree.core.management.commands._close_keyword_gate import _scan_sources, _suggest_rewrite
from teatree.core.models import Session, Ticket, Worktree
from teatree.utils import git as git_mod
from teatree.utils import git_commit as git_commit_mod
from tests.teatree_core.management_commands._overlays import (
    FORBID_CLOSE_KEYWORDS_OVERLAY,
    FULL_OVERLAY,
    SETTINGS,
    _patch_overlays,
)

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


def _shippable_ticket(*, repo: str = "/tmp/wt", branch: str = "feature-x") -> Ticket:
    ticket = Ticket.objects.create(
        overlay="test",
        state=Ticket.State.REVIEWED,
        issue_url="https://example.com/issues/70",
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

    ``last_commit_message`` is the raw MR-description source the gate scans
    (and also what ``ship_preview`` derives the post-sanitize description
    from); ``commit_messages`` feeds the branch-commit scan; ``default_branch``
    lets the ``origin/main..branch`` range be built. Visual QA is the other
    unstoppable boundary (browser).
    """
    with (
        patch.object(pr_mod, "_run_visual_qa_gate", return_value=None),
        patch(
            "teatree.core.management.commands._close_keyword_gate.git.last_commit_message",
            return_value=(subject, body),
        ),
        patch(
            "teatree.core.management.commands._pr_preview.git.last_commit_message",
            return_value=(subject, body),
        ),
        patch(
            "teatree.core.management.commands._close_keyword_gate.git.default_branch",
            return_value="main",
        ),
        patch(
            "teatree.core.management.commands._close_keyword_gate.git.commit_messages",
            return_value=list(commit_bodies or []),
        ),
    ):
        yield


class TestCloseKeywordGateForbiddenOverlay(TestCase):
    """Overlay with ``forbid_close_keywords=True``: the gate rejects."""

    @_patch_overlays(FORBID_CLOSE_KEYWORDS_OVERLAY)
    @override_settings(**SETTINGS)
    def test_rejects_closes_in_mr_description(self) -> None:
        ticket = _shippable_ticket()
        with pytest.raises(SystemExit) as ctx, _git_boundary(subject="Closes downstream-product#1632."):
            call_command("pr", "create", str(ticket.pk))
        assert ctx.value.code != 0
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED

    @_patch_overlays(FORBID_CLOSE_KEYWORDS_OVERLAY)
    @override_settings(**SETTINGS)
    def test_rejects_fixes_in_commit_body(self) -> None:
        ticket = _shippable_ticket()
        with (
            pytest.raises(SystemExit) as ctx,
            _git_boundary(
                subject="feat: clean subject",
                commit_bodies=["feat: clean subject\n\nFixes #1632"],
            ),
        ):
            call_command("pr", "create", str(ticket.pk))
        assert ctx.value.code != 0
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED

    @_patch_overlays(FORBID_CLOSE_KEYWORDS_OVERLAY)
    @override_settings(**SETTINGS)
    def test_error_message_names_offender_and_suggests_rewrite(self) -> None:
        ticket = _shippable_ticket()
        url = "https://gitlab.com/eng-group/downstream-product/-/issues/9"
        with (
            pytest.raises(SystemExit) as ctx,
            _git_boundary(subject=f"Resolves {url}"),
        ):
            call_command("pr", "create", str(ticket.pk))
        message = str(ctx.value)
        assert f"Resolves {url}" in message
        assert f"Relates to {url}" in message

    @_patch_overlays(FORBID_CLOSE_KEYWORDS_OVERLAY)
    @override_settings(**SETTINGS)
    def test_relates_to_and_see_pass(self) -> None:
        ticket = _shippable_ticket()
        with _git_boundary(
            subject="Relates to downstream-product#1632.",
            commit_bodies=["Relates to downstream-product#1632.\n\nSee downstream-product#99"],
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk)))
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert "error" not in result

    @_patch_overlays(FORBID_CLOSE_KEYWORDS_OVERLAY)
    @override_settings(**SETTINGS)
    def test_all_keyword_variants_rejected(self) -> None:
        for keyword in ("Close", "Closes", "Closed", "fix", "Fixes", "fixed", "resolve", "Resolves", "RESOLVED"):
            ticket = _shippable_ticket()
            with (
                pytest.raises(SystemExit),
                _git_boundary(subject="feat: x", body=f"{keyword} #42"),
            ):
                call_command("pr", "create", str(ticket.pk))
            ticket.delete()

    @_patch_overlays(FORBID_CLOSE_KEYWORDS_OVERLAY)
    @override_settings(**SETTINGS)
    def test_rejects_colon_form_closes(self) -> None:
        """#1090: GitLab auto-closes the colon form, so the gate must reject it."""
        for subject in ("Closes: downstream-product#1632.", "Fixes:  #1632"):
            ticket = _shippable_ticket()
            with (
                pytest.raises(SystemExit) as ctx,
                _git_boundary(subject=subject),
            ):
                call_command("pr", "create", str(ticket.pk))
            assert ctx.value.code != 0
            ticket.delete()
        assert _suggest_rewrite("Closes: downstream-product#1632.") == "Relates to downstream-product#1632."


class TestCloseKeywordGateNonForbiddenOverlay(TestCase):
    """teatree-style overlay (default ``forbid_close_keywords=False``): allowed."""

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_closes_allowed_for_teatree_overlay(self) -> None:
        ticket = _shippable_ticket()
        with _git_boundary(
            subject="Closes #1012",
            commit_bodies=["Closes #1012\n\nFixes #999"],
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk)))
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert "error" not in result


class TestScanSourcesUnverifiable(TestCase):
    """``_scan_sources`` skips sources it cannot read rather than blocking."""

    def test_no_repo_or_branch_returns_empty(self) -> None:
        worktree = Worktree(overlay="test", repo_path="", branch="")
        assert _scan_sources(worktree) == []

    def test_git_failure_returns_partial_sources(self) -> None:
        worktree = Worktree(overlay="test", repo_path="/tmp/wt", branch="feat")
        with (
            patch.object(gate_mod.git, "last_commit_message", return_value=("feat: x", "")),
            patch.object(
                gate_mod.git,
                "default_branch",
                side_effect=gate_mod.CommandFailedError(["git", "branch"], 128, "", "boom"),
            ),
        ):
            # The last-commit source was collected before the failure; the
            # branch-range scan is skipped (unverifiable, not a block).
            assert _scan_sources(worktree) == ["feat: x"]


class TestCommitMessagesHelper(TestCase):
    """``git.commit_messages`` — pure parsing over the ``git log`` boundary."""

    def test_empty_range_returns_empty(self) -> None:
        assert git_mod.commit_messages(repo="/tmp/wt", range_spec="") == []

    def test_splits_on_record_separator_keeping_multiline_bodies(self) -> None:
        raw = "feat: a\n\nbody line 1\n\nbody line 2\x1e\nfix: b\n\nFixes #1\x1e\n"
        with patch.object(git_commit_mod, "run", return_value=raw):
            assert git_mod.commit_messages(repo="/tmp/wt", range_spec="origin/main..feat") == [
                "feat: a\n\nbody line 1\n\nbody line 2",
                "fix: b\n\nFixes #1",
            ]
