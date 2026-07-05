from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.backend_protocols import BackendResolutionError, PrOpenState
from teatree.core.gates import pr_budget_gate
from teatree.core.gates.orphan_guard import BranchReport, BranchStatus
from teatree.core.management.commands import _ensure_pr as ensure_pr_mod
from teatree.core.management.commands import pr as pr_command
from teatree.core.management.commands._ensure_pr import create_or_defer_pr
from teatree.core.models import PullRequest, Ticket, Worktree
from tests.teatree_core.cleanup._shared import _run_git

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
        # #1222 / #1226: ``web_url`` is the canonical cross-host key
        # (GitHub backend was aligned to it; GitLab API native).
        host.create_pr.return_value = {"web_url": "https://github.com/souliane/teatree/pull/999"}
        host.current_user.return_value = "souliane"
        self._monkeypatch.setattr(ensure_pr_mod, "code_host_for_repo_from_overlay", lambda _repo_path: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-q"),
            patch.object(ensure_pr_mod.git, "remote_url", return_value="git@github.com:souliane/teatree.git"),
            patch.object(ensure_pr_mod, "_branch_own_commit_message", return_value=("feat: cool thing", "body")),
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

    def test_create_url_failing_reread_is_reported_as_error_not_a_url(self) -> None:
        """#1194: a create URL whose independent re-read 404s is reported failed.

        ``create_pr`` returned a well-formed URL, but a fresh GET reports
        ``UNKNOWN`` — the create silently no-op'd. ``ensure-pr`` must surface an
        ``error`` and no ``url`` so the orphan-branch path never hands back a
        phantom PR.
        """
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://github.com/souliane/teatree/pull/phantom"}
        host.current_user.return_value = "souliane"
        host.get_pr_open_state.return_value = PrOpenState.UNKNOWN
        self._monkeypatch.setattr(ensure_pr_mod, "code_host_for_repo_from_overlay", lambda _repo_path: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-q"),
            patch.object(ensure_pr_mod.git, "remote_url", return_value="git@github.com:souliane/teatree.git"),
            patch.object(ensure_pr_mod, "_branch_own_commit_message", return_value=("feat: cool thing", "body")),
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

        assert "url" not in result
        assert "verify-by-re-read" in str(result["error"])

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
        self._monkeypatch.setattr(ensure_pr_mod, "code_host_for_repo_from_overlay", lambda _repo_path: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-q"),
            patch.object(ensure_pr_mod.git, "remote_url", return_value="git@github.com:souliane/teatree.git"),
            patch.object(ensure_pr_mod, "_branch_own_commit_message", return_value=("feat: cool thing", "body")),
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

    def test_repo_flag_with_forge_slug_rejected_before_touching_classification(self) -> None:
        """#2937.

        ``--repo owner/repo`` (a forge slug, not a filesystem path) must fail
        loud with a clear, actionable error — and never reach branch classification,
        the path that used to silently misreport the branch as SYNCED.
        """
        with patch.object(pr_command, "classify_branch") as mock_classify:
            result = cast(
                "dict[str, object]",
                call_command("pr", "ensure-pr", repo="owner/repo", branch="feature"),
            )

        assert "error" in result
        assert "owner/repo" in str(result["error"])
        mock_classify.assert_not_called()

    def test_classify_branch_git_failure_surfaces_as_structured_error(self) -> None:
        """#2937.

        A real git failure during classification must surface as a
        structured error, never an unhandled exception or a silent SYNCED skip.
        """
        host = MagicMock()
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        from teatree.utils.run import CommandFailedError  # noqa: PLC0415

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-r"),
            patch.object(
                pr_command,
                "classify_branch",
                side_effect=CommandFailedError(
                    cmd=["git", "-C", ".", "log", "feat-r", "--not", "origin/main"],
                    returncode=128,
                    stdout="",
                    stderr="fatal: ambiguous argument 'origin/main': unknown revision",
                ),
            ),
        ):
            result = cast("dict[str, object]", call_command("pr", "ensure-pr"))

        assert "error" in result
        assert "feat-r" in str(result["error"])
        host.create_pr.assert_not_called()

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
        self._monkeypatch.setattr(ensure_pr_mod, "code_host_for_repo_from_overlay", lambda _repo_path: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-q"),
            patch.object(ensure_pr_mod.git, "remote_url", return_value="git@github.com:souliane/teatree.git"),
            patch.object(ensure_pr_mod, "_branch_own_commit_message", return_value=("feat: x", "body")),
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

    def test_refuses_when_ticket_is_at_its_open_pr_budget(self) -> None:
        """North-star PR-2: the orphan path refuses before creating when at budget."""
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW)
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path=".", branch="feat-q")
        PullRequest.objects.create(
            ticket=ticket,
            url="https://github.com/souliane/teatree/pull/1",
            repo="souliane/teatree",
            iid="1",
            overlay="test",
        )
        host = MagicMock()
        host.current_user.return_value = "souliane"
        self._monkeypatch.setattr(ensure_pr_mod, "code_host_for_repo_from_overlay", lambda _repo_path: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-q"),
            patch.object(ensure_pr_mod.git, "remote_url", return_value="git@github.com:souliane/teatree.git"),
            patch.object(ensure_pr_mod, "_branch_own_commit_message", return_value=("feat: x", "body")),
            patch.object(
                pr_budget_gate,
                "get_effective_settings",
                return_value=UserSettings(max_open_prs_per_repo_per_ticket=1),
            ),
            patch.object(
                pr_command,
                "classify_branch",
                return_value=BranchReport(repo=".", branch="feat-q", status=BranchStatus.PUSHED_ORPHAN, ahead_count=3),
            ),
        ):
            result = cast("dict[str, object]", call_command("pr", "ensure-pr"))

        assert "max_open_prs_per_repo_per_ticket" in str(result["error"])
        assert "souliane/teatree/pull/1" in str(result["error"])
        host.create_pr.assert_not_called()


class TestCreatePrTitleSourcing(TestCase):
    """#1534: the PR title/body must come from the branch's OWN commit.

    Real git under ``tmp_path`` — only the forge ``create_pr`` is mocked.
    ``origin/main`` carries an unrelated, already-merged commit ``M``; the
    feature branch carries its own work ``B``. The repo's WORKING TREE is left
    checked out on the default branch at ``M`` (the main-clone / wrong-ref /
    slug condition #1534 describes), so the former ``HEAD``-based sourcing
    would title the PR after ``M``. The opened PR must be titled after ``B``.
    """

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._monkeypatch = monkeypatch
        self._tmp_path = tmp_path

    @staticmethod
    def _set_identity(clone: Path) -> None:
        """Give the tmp clone a commit identity (the Docker CI image has none)."""
        _run_git("config", "user.email", "t@t", cwd=clone)
        _run_git("config", "user.name", "t", cwd=clone)

    def _origin_and_feature(self, branch_commits: list[str], *, default_branch: str = "main") -> Path:
        origin = self._tmp_path / "origin.git"
        _run_git("init", "-q", "--bare", "-b", default_branch, str(origin), cwd=self._tmp_path)
        clone = self._tmp_path / "clone"
        _run_git("clone", "-q", str(origin), str(clone), cwd=self._tmp_path)
        self._set_identity(clone)
        _run_git("commit", "--allow-empty", "-q", "-m", "feat(lifecycle): unrelated already-merged (#1426)", cwd=clone)
        # M (the unrelated, already-merged head) IS origin/<default>. The
        # branch is built on a side ref and the working tree is then returned
        # to the default branch at M — the main-clone / wrong-ref condition
        # where HEAD-based sourcing wrongly picks M.
        _run_git("push", "-q", "origin", default_branch, cwd=clone)
        # Point ``origin`` at the GitHub slug so the spec carries the real
        # repo; the bare-origin push above already populated origin/<default>.
        _run_git("remote", "set-url", "origin", "git@github.com:souliane/teatree.git", cwd=clone)
        _run_git("checkout", "-q", "-b", "1534-fix-the-real-work", cwd=clone)
        for subject in branch_commits:
            _run_git("commit", "--allow-empty", "-q", "-m", subject, cwd=clone)
        _run_git("checkout", "-q", default_branch, cwd=clone)
        return clone

    def _host(self) -> MagicMock:
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://github.com/souliane/teatree/pull/1"}
        host.current_user.return_value = "souliane"
        self._monkeypatch.setattr(ensure_pr_mod, "code_host_for_repo_from_overlay", lambda _repo_path: host)
        return host

    def test_title_derives_from_branch_commit_not_default_head(self) -> None:
        clone = self._origin_and_feature(["fix(y): the real work"])
        host = self._host()

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = create_or_defer_pr(str(clone), "1534-fix-the-real-work")

        assert result["url"] == "https://github.com/souliane/teatree/pull/1"
        (spec,) = host.create_pr.call_args.args
        assert spec.title == "fix(y): the real work"
        assert "1426" not in spec.title
        assert "1426" not in spec.description

    def test_oldest_unique_commit_is_the_title_for_multi_commit_branch(self) -> None:
        clone = self._origin_and_feature(["fix(y): the real work", "fix(y): a follow-up commit", "fix(y): one more"])
        host = self._host()

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            create_or_defer_pr(str(clone), "1534-fix-the-real-work")

        (spec,) = host.create_pr.call_args.args
        assert spec.title == "fix(y): the real work"

    def test_no_unique_commit_falls_back_to_wip_not_default_head(self) -> None:
        """Guard: a branch with no commits over origin/<default> must NOT title after M."""
        origin = self._tmp_path / "origin.git"
        _run_git("init", "-q", "--bare", "-b", "main", str(origin), cwd=self._tmp_path)
        clone = self._tmp_path / "clone"
        _run_git("clone", "-q", str(origin), str(clone), cwd=self._tmp_path)
        self._set_identity(clone)
        _run_git("commit", "--allow-empty", "-q", "-m", "feat(lifecycle): unrelated already-merged (#1426)", cwd=clone)
        _run_git("push", "-q", "origin", "main", cwd=clone)
        _run_git("remote", "set-url", "origin", "git@github.com:souliane/teatree.git", cwd=clone)
        # Feature branch sits exactly on origin/main — zero unique commits.
        _run_git("checkout", "-q", "-b", "empty-branch", cwd=clone)
        host = self._host()

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            create_or_defer_pr(str(clone), "empty-branch")

        (spec,) = host.create_pr.call_args.args
        assert spec.title == "WIP: empty-branch"
        assert "1426" not in spec.title


class TestEnsurePrResolutionError:
    """#2025: a mismatched forge surfaces as a structured error result.

    The central AC: ``create_or_defer_pr`` must return a structured
    ``error`` result — never an unhandled exception — when the repo's
    forge has no configured credentials.
    """

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_returns_structured_error_on_resolution_failure(self) -> None:
        def _raise(_repo_path: str) -> object:
            msg = "repo origin resolves to the gitlab forge but no gitlab token"
            raise BackendResolutionError(msg)

        self._monkeypatch.setattr(ensure_pr_mod, "code_host_for_repo_from_overlay", _raise)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = create_or_defer_pr(".", "some-branch")

        assert "error" in result
        assert "gitlab" in result["error"]
