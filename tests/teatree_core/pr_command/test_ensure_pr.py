from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import _ensure_pr as ensure_pr_mod
from teatree.core.management.commands import pr as pr_command
from teatree.core.orphan_guard import BranchReport, BranchStatus

from ._shared import _MOCK_OVERLAY


class TestEnsurePr(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_no_op_when_branch_has_open_pr(self) -> None:
        host = MagicMock()
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-x"),
            patch.object(
                pr_command,
                "classify_branch",
                return_value=BranchReport(
                    repo=".",
                    branch="feat-x",
                    status=BranchStatus.OPEN_PR,
                    ahead_count=3,
                    open_pr_url="https://gitlab.com/org/repo/-/merge_requests/42",
                ),
            ),
        ):
            result = cast("dict[str, object]", call_command("pr", "ensure-pr"))

        assert result["skipped"] == "open PR exists"
        assert "42" in str(result["url"])
        host.create_pr.assert_not_called()

    def test_no_op_when_branch_synced(self) -> None:
        host = MagicMock()
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-y"),
            patch.object(
                pr_command,
                "classify_branch",
                return_value=BranchReport(
                    repo=".",
                    branch="feat-y",
                    status=BranchStatus.SYNCED,
                    ahead_count=0,
                ),
            ),
        ):
            result = cast("dict[str, object]", call_command("pr", "ensure-pr"))

        assert "synced" in str(result["skipped"])
        host.create_pr.assert_not_called()

    def test_defers_when_branch_not_on_remote(self) -> None:
        host = MagicMock()
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-z"),
            patch.object(
                pr_command,
                "classify_branch",
                return_value=BranchReport(
                    repo=".",
                    branch="feat-z",
                    status=BranchStatus.UNPUSHED_ORPHAN,
                    ahead_count=2,
                ),
            ),
        ):
            result = cast("dict[str, object]", call_command("pr", "ensure-pr"))

        assert "not on remote yet" in str(result["skipped"])
        assert "feat-z" in str(result["hint"])
        host.create_pr.assert_not_called()

    def test_creates_pr_when_pushed_orphan(self) -> None:
        host = MagicMock()
        host.create_pr.return_value = {"url": "https://github.com/souliane/teatree/pull/999"}
        host.current_user.return_value = "souliane"
        self._monkeypatch.setattr(ensure_pr_mod, "code_host_from_overlay", lambda: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-q"),
            patch.object(ensure_pr_mod.git, "remote_url", return_value="git@github.com:souliane/teatree.git"),
            patch.object(ensure_pr_mod.git, "last_commit_message", return_value=("feat: cool thing", "body")),
            patch.object(
                pr_command,
                "classify_branch",
                return_value=BranchReport(
                    repo=".",
                    branch="feat-q",
                    status=BranchStatus.PUSHED_ORPHAN,
                    ahead_count=5,
                ),
            ),
        ):
            result = cast("dict[str, object]", call_command("pr", "ensure-pr"))

        assert result["url"] == "https://github.com/souliane/teatree/pull/999"
        assert result["branch"] == "feat-q"
        (spec,) = host.create_pr.call_args.args
        assert spec.draft is False
        assert spec.branch == "feat-q"
        assert spec.repo == "souliane/teatree"
        assert spec.title == "feat: cool thing"

    def test_defers_when_remote_ref_stale_in_pre_push_race(self) -> None:
        """#792: ensure-pr must defer (not raise) on the pre-push stale-remote race.

        ensure-pr runs in the PRE-push hook. The remote branch ref exists at
        an older base (classify_branch => PUSHED_ORPHAN), but THIS push has
        not landed yet, so the forge rejects PR creation with "No commits
        between main and <branch>". Hard-failing aborts the very push that
        would make the PR creatable — a permanent deadlock. ensure-pr must
        DEFER (skip + exit 0) so the push proceeds and the post-push
        ensure-pr opens the PR.
        """
        from teatree.utils.run import CommandFailedError  # noqa: PLC0415

        host = MagicMock()
        host.current_user.return_value = "souliane"
        host.create_pr.side_effect = CommandFailedError(
            cmd=["gh", "pr", "create"],
            returncode=1,
            stdout="",
            stderr="pull request create failed: GraphQL: No commits between main and feat-q (createPullRequest)",
        )
        self._monkeypatch.setattr(ensure_pr_mod, "code_host_from_overlay", lambda: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-q"),
            patch.object(ensure_pr_mod.git, "remote_url", return_value="git@github.com:souliane/teatree.git"),
            patch.object(ensure_pr_mod.git, "last_commit_message", return_value=("feat: cool thing", "body")),
            patch.object(
                pr_command,
                "classify_branch",
                return_value=BranchReport(
                    repo=".",
                    branch="feat-q",
                    status=BranchStatus.PUSHED_ORPHAN,
                    ahead_count=3,
                ),
            ),
        ):
            result = cast("dict[str, object]", call_command("pr", "ensure-pr"))

        # Deferred, not raised: the push must be allowed to proceed.
        assert "pre-push race" in str(result["skipped"])
        assert "feat-q" in str(result["hint"])
        host.create_pr.assert_called_once()

    def test_other_create_pr_failure_still_raises(self) -> None:
        """A non-race create_pr failure must still surface (no silent swallow)."""
        from teatree.utils.run import CommandFailedError  # noqa: PLC0415

        host = MagicMock()
        host.current_user.return_value = "souliane"
        host.create_pr.side_effect = CommandFailedError(
            cmd=["gh", "pr", "create"],
            returncode=1,
            stdout="",
            stderr="pull request create failed: GraphQL: API rate limit exceeded",
        )
        self._monkeypatch.setattr(ensure_pr_mod, "code_host_from_overlay", lambda: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-q"),
            patch.object(ensure_pr_mod.git, "remote_url", return_value="git@github.com:souliane/teatree.git"),
            patch.object(ensure_pr_mod.git, "last_commit_message", return_value=("feat: x", "body")),
            patch.object(
                pr_command,
                "classify_branch",
                return_value=BranchReport(
                    repo=".",
                    branch="feat-q",
                    status=BranchStatus.PUSHED_ORPHAN,
                    ahead_count=3,
                ),
            ),
            pytest.raises(CommandFailedError, match="rate limit"),
        ):
            call_command("pr", "ensure-pr")
