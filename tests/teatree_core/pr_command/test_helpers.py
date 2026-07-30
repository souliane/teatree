from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree import visual_qa
from teatree.core.management.commands._ship.gates import resolve_base_url as _resolve_base_url
from teatree.core.management.commands.pr import _assert_commits_ahead_of_base, _check_shipping_gate, _run_visual_qa_gate
from teatree.core.models import Session, Ticket, Worktree

from ._shared import _MOCK_OVERLAY


class TestCheckShippingGate(TestCase):
    def test_returns_structured_failure_when_no_session(self) -> None:
        # #694 nit 1: no session => no attested work. The gate must return a
        # structured failure (not None), otherwise ``ship()`` raises a raw
        # TransitionNotAllowed from a non-REVIEWED state.
        ticket = Ticket.objects.create()
        result = _check_shipping_gate(ticket)
        assert result is not None
        assert result["allowed"] is False
        assert result["missing"] == ["testing", "reviewing"]

    def test_returns_none_when_gate_passes(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.visit_phase("retro")
        assert _check_shipping_gate(ticket) is None

    def test_returns_structured_error_with_missing_phases(self) -> None:
        ticket = Ticket.objects.create()
        Session.objects.create(ticket=ticket)

        result = _check_shipping_gate(ticket)

        assert result is not None
        assert result["allowed"] is False
        assert "reviewing" in result["missing"]
        assert "testing" in result["missing"]
        assert "hint" in result


class TestResolveBaseUrl(TestCase):
    def test_returns_default_when_worktree_is_none(self) -> None:
        assert _resolve_base_url(None) == "http://127.0.0.1:8000"

    def test_prefers_frontend_url(self) -> None:
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/wt",
            branch="feat",
            extra={"urls": {"frontend": "http://localhost:4201", "backend": "http://localhost:8001"}},
        )
        assert _resolve_base_url(worktree) == "http://localhost:4201"

    def test_falls_back_to_backend(self) -> None:
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/wt",
            branch="feat",
            extra={"urls": {"backend": "http://localhost:8001"}},
        )
        assert _resolve_base_url(worktree) == "http://localhost:8001"

    def test_falls_back_to_localhost_when_no_urls(self) -> None:
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(ticket=ticket, repo_path="/tmp/wt", branch="feat")
        assert _resolve_base_url(worktree) == "http://127.0.0.1:8000"


class TestRunVisualQAGate(TestCase):
    def _ticket(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/77")
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="/tmp/wt", branch="feat-x")
        return ticket

    def test_skipped_run_does_not_pollute_extra(self) -> None:
        ticket = self._ticket()
        clean = visual_qa.VisualQAReport(targets=[], skipped_reason="no frontend changes")
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", return_value=clean),
        ):
            assert _run_visual_qa_gate(ticket) is None

        ticket.refresh_from_db()
        assert "visual_qa" not in ticket.extra

    def test_records_summary_when_pages_checked(self) -> None:
        ticket = self._ticket()
        page = visual_qa.PageResult(url="http://x/", screenshot_path=".t3/visual_qa/00-root.png")
        report = visual_qa.VisualQAReport(targets=["/"], pages=[page], base_url="http://x")
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", return_value=report),
        ):
            assert _run_visual_qa_gate(ticket) is None

        ticket.refresh_from_db()
        assert ticket.extra["visual_qa"]["pages_checked"] == 1
        assert ticket.extra["visual_qa"]["errors"] == 0

    def test_returns_error_when_findings(self) -> None:
        ticket = self._ticket()
        page = visual_qa.PageResult(
            url="http://x/",
            errors=[visual_qa.PageError(url="http://x/", kind="page", message="boom")],
        )
        report = visual_qa.VisualQAReport(targets=["/"], pages=[page], base_url="http://x")
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", return_value=report),
        ):
            result = _run_visual_qa_gate(ticket)

        assert result is not None
        assert result["allowed"] is False
        assert "1 blocking finding" in result["error"]
        assert "## Visual QA" in result["report_markdown"]

        ticket.refresh_from_db()
        assert ticket.extra["visual_qa"]["errors"] == 1

    def test_resolves_invoking_worktree_not_stale_first(self) -> None:
        """#776 N1: visual QA inspects the invoking workstream's repo.

        Same ``worktrees.first()`` root cause as the ship-branch fix,
        same path, residual on the reused-multi-workstream ticket #776
        targets. With ``ship_invoking_branch`` recorded, the gate must
        scan the matching worktree's repo, not the stale earliest row.
        """
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/776")
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="/tmp/repo-pr-a", branch="s-776-pr-a-merged")
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="/tmp/repo-pr-b", branch="s-776-pr-b-current")
        ticket.extra = {"ship_invoking_branch": "s-776-pr-b-current"}
        ticket.save(update_fields=["extra"])
        captured: dict[str, str] = {}

        def fake_changed_files(*, repo: str) -> list[str]:
            captured["repo"] = repo
            return []

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "changed_files", side_effect=fake_changed_files),
            patch.object(
                visual_qa, "evaluate", return_value=visual_qa.VisualQAReport(targets=[], skipped_reason="none")
            ),
        ):
            assert _run_visual_qa_gate(ticket) is None

        # The invoking PR-B repo is scanned — NOT the stale PR-A first() row.
        assert captured["repo"] == "/tmp/repo-pr-b"

    def test_skip_reason_propagates(self) -> None:
        ticket = self._ticket()
        captured: dict[str, str] = {}

        def fake_evaluate(**kwargs: object) -> visual_qa.VisualQAReport:
            captured["skip_reason"] = str(kwargs.get("skip_reason", ""))
            return visual_qa.VisualQAReport(targets=[], skipped_reason="--skip: my reason")

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", side_effect=fake_evaluate),
        ):
            assert _run_visual_qa_gate(ticket, skip_reason="my reason") is None

        assert captured["skip_reason"] == "my reason"


class TestAssertCommitsAheadOfBase(TestCase):
    """#788 helper unit branches (real tmp git repo per the doctrine)."""

    @staticmethod
    def _git(repo: str, *args: str) -> None:
        from teatree.utils import run as run_mod  # noqa: PLC0415

        run_mod.run_checked(["git", "-C", repo, *args])

    def _wt(self, repo_path: str, branch: str) -> Worktree:
        t = Ticket.objects.create(overlay="test")
        return Worktree.objects.create(
            ticket=t, overlay="test", repo_path=repo_path, branch=branch, extra={"worktree_path": repo_path}
        )

    def test_branch_with_a_commit_ahead_returns_none(self) -> None:
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as root:
            self._git(root, "init", "-q", "-b", "main")
            self._git(root, "config", "user.email", "t@example.com")
            self._git(root, "config", "user.name", "t")
            (Path(root) / "a").write_text("1", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-q", "-m", "base")
            self._git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
            self._git(root, "checkout", "-q", "-b", "feat-ahead")
            (Path(root) / "b").write_text("2", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-q", "-m", "ahead")  # 1 commit ahead of base

            assert _assert_commits_ahead_of_base(self._wt(root, "feat-ahead")) is None

    def test_missing_repo_or_branch_returns_none(self) -> None:
        assert _assert_commits_ahead_of_base(self._wt("", "feat")) is None
        assert _assert_commits_ahead_of_base(self._wt("/tmp/x", "")) is None

    def test_confirmed_zero_returns_structured_error(self) -> None:
        """Branch at exact parity with base (0 commits ahead) → block contract.

        Neutering the confirmed-zero arm (e.g. ``ahead > 0`` flipped, or
        the ``return NoCommitsAheadError`` removed) makes this RED — the
        guard's whole reason to exist (#788).
        """
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as root:
            self._git(root, "init", "-q", "-b", "main")
            self._git(root, "config", "user.email", "t@example.com")
            self._git(root, "config", "user.name", "t")
            (Path(root) / "a").write_text("1", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-q", "-m", "base")
            self._git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
            # Branch points at the SAME commit as origin/main → 0 ahead.
            self._git(root, "checkout", "-q", "-b", "feat-parity")

            result = _assert_commits_ahead_of_base(self._wt(root, "feat-parity"))

        assert result is not None, "confirmed-zero MUST block (returns NoCommitsAheadError)"
        assert result["branch"] == "feat-parity"
        assert result["base"] == "origin/main"
        assert "0 commits ahead" in result["error"]

    def test_unverifiable_git_error_returns_none_proceeds(self) -> None:
        """No-block-on-unknown safety contract (#788's make-or-break fail-direction).

        When git introspection cannot be performed — ``default_branch``
        raising :class:`CommandFailedError`, ``rev_count`` raising, or an
        ``int()`` ``ValueError`` — the state is *unverifiable*, distinct
        from the confirmed-zero bug, so the guard MUST return ``None``
        (ship proceeds, prior behaviour preserved). Neutering this arm
        (e.g. ``except`` block returning the error, or removed) makes
        this RED. Real on-disk repo so only the failing primitive is
        mocked.
        """
        import tempfile  # noqa: PLC0415

        from teatree.utils import git as git_mod  # noqa: PLC0415
        from teatree.utils.run import CommandFailedError  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as root:
            self._git(root, "init", "-q", "-b", "main")
            self._git(root, "config", "user.email", "t@example.com")
            self._git(root, "config", "user.name", "t")
            (Path(root) / "a").write_text("1", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-q", "-m", "base")
            self._git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
            wt = self._wt(root, "main")

            with patch.object(git_mod, "default_branch", side_effect=CommandFailedError(["git"], 1, "", "boom")):
                assert _assert_commits_ahead_of_base(wt) is None, (
                    "default_branch failure is unverifiable → MUST proceed (None)"
                )

            with patch.object(git_mod, "rev_count", side_effect=RuntimeError("git exploded")):
                assert _assert_commits_ahead_of_base(wt) is None, (
                    "rev_count failure is unverifiable → MUST proceed (None)"
                )

            with patch.object(git_mod, "rev_count", side_effect=ValueError("not an int")):
                assert _assert_commits_ahead_of_base(wt) is None, (
                    "rev_count ValueError is unverifiable → MUST proceed (None)"
                )

    def test_unverifiable_git_error_logs_a_warning(self) -> None:
        """F3.3: the fail-open path leaves a warning breadcrumb, not silence.

        A git-introspection failure looks exactly like a clean pass on the CLI;
        keeping #788's fail-open posture is right, but it must now log a warning
        naming the branch/repo so a mistaken SHIPPED whose hollowness could not
        be confirmed is traceable.
        """
        import tempfile  # noqa: PLC0415

        from teatree.utils import git as git_mod  # noqa: PLC0415
        from teatree.utils.run import CommandFailedError  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as root:
            self._git(root, "init", "-q", "-b", "main")
            self._git(root, "config", "user.email", "t@example.com")
            self._git(root, "config", "user.name", "t")
            (Path(root) / "a").write_text("1", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-q", "-m", "base")
            self._git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
            wt = self._wt(root, "main")

            with (
                patch.object(git_mod, "default_branch", side_effect=CommandFailedError(["git"], 1, "", "boom")),
                self.assertLogs("teatree.core.management.commands._ship.gates", level="WARNING") as logs,
            ):
                assert _assert_commits_ahead_of_base(wt) is None
        joined = "\n".join(logs.output)
        assert "could not verify commits ahead of base" in joined
        assert "main" in joined
