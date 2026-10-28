import socket
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase
from playwright.sync_api import Error as PlaywrightError

from teatree import visual_qa
from teatree.core.management.commands._e2e_resolvers import resolve_visual_qa_base_url
from teatree.core.management.commands.pr import _assert_commits_ahead_of_base, _check_shipping_gate, _run_visual_qa_gate
from teatree.core.models import Session, Ticket, Worktree
from teatree.core.overlay import OverlayRuntime
from teatree.types import RunCommand
from tests._git_repo import make_git_repo, run_git

from ._shared import _MOCK_OVERLAY


class TestCheckShippingGate(TestCase):
    def test_returns_structured_failure_when_no_session(self) -> None:
        # #694 nit 1: no session => no attested work. The gate must return a
        # structured failure (not None), otherwise ``ship()`` raises a raw
        # TransitionNotAllowed from a non-SELF_REVIEWED state.
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


class TestLiveVisualQAUrl(TestCase):
    def test_an_unrelated_4200_listener_is_not_this_branches_server(self) -> None:
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(ticket=ticket, repo_path="/tmp/wt", branch="feat")
        with (
            patch("teatree.core.management.commands._e2e_resolvers._discover_frontend_port", return_value=4200),
            patch("teatree.core.management.commands._e2e_resolvers._disc.compose_frontend_port", return_value=None),
            pytest.raises(RuntimeError, match="no server for this branch"),
        ):
            resolve_visual_qa_base_url(worktree)

    def test_local_listener_stays_in_the_browser_namespace(self) -> None:
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(ticket=ticket, repo_path="/tmp/wt", branch="feat")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            with patch(
                "teatree.core.management.commands._e2e_resolvers._disc.compose_frontend_port", return_value=port
            ):
                assert resolve_visual_qa_base_url(worktree) == f"http://127.0.0.1:{port}"

    def test_ipv6_only_listener_uses_ipv6_url(self) -> None:
        if not socket.has_ipv6:
            self.skipTest("no IPv6 support")
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(ticket=ticket, repo_path="/tmp/wt", branch="feat")
        try:
            listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            listener.bind(("::1", 0))
            listener.listen()
        except OSError:
            self.skipTest("no IPv6 loopback")
        with listener:
            port = listener.getsockname()[1]
            with patch(
                "teatree.core.management.commands._e2e_resolvers._disc.compose_frontend_port", return_value=port
            ):
                assert resolve_visual_qa_base_url(worktree) == f"http://[::1]:{port}"

    def test_discovers_the_live_port_in_the_browser_venue(self) -> None:
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/wt", branch="feat", extra={"urls": {"frontend": "http://wrong:8000"}}
        )
        with (
            patch("teatree.core.management.commands._e2e_resolvers._disc.compose_frontend_port", return_value=4201),
            patch("teatree.core.management.commands._e2e_resolvers._disc.detect_local_address", return_value=None),
            patch(
                "teatree.core.management.commands._e2e_resolvers.host_published_port_host",
                return_value="host.docker.internal",
            ),
        ):
            assert resolve_visual_qa_base_url(worktree) == "http://host.docker.internal:4201"

    def test_missing_live_port_is_did_not_run(self) -> None:
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(ticket=ticket, repo_path="/tmp/wt", branch="feat")
        with (
            patch("teatree.core.management.commands._e2e_resolvers._disc.compose_frontend_port", return_value=None),
            pytest.raises(RuntimeError, match="no server for this branch"),
        ):
            resolve_visual_qa_base_url(worktree)


class _PageTargetReview:
    def visual_qa_targets(self, changed_files: list[str]) -> list[str]:
        return ["/"] if changed_files else []


class _PageTargetOverlay:
    review = _PageTargetReview()
    runtime = OverlayRuntime()


def _playwright_unavailable(*_args: object, **_kwargs: object) -> list[visual_qa.PageResult]:
    msg = "BrowserType.launch: Executable doesn't exist"
    raise visual_qa.VisualQAUnavailableError(msg)


class TestRunVisualQAGate(TestCase):
    def setUp(self) -> None:
        live_port = patch(
            "teatree.core.management.commands._e2e_resolvers._disc.compose_frontend_port", return_value=8000
        )
        host = patch(
            "teatree.core.management.commands._e2e_resolvers.host_published_port_host", return_value="127.0.0.1"
        )
        live_port.start()
        host.start()
        self.addCleanup(live_port.stop)
        self.addCleanup(host.stop)

    def _ticket_and_worktree(self) -> tuple[Ticket, Worktree]:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/77")
        worktree = Worktree.objects.create(ticket=ticket, overlay="test", repo_path="/tmp/wt", branch="feat-x")
        return ticket, worktree

    def test_a_browser_that_cannot_start_refuses_as_did_not_run(self) -> None:
        ticket, worktree = self._ticket_and_worktree()
        with (
            patch("teatree.core.management.commands._ship.gates.get_overlay", return_value=_PageTargetOverlay()),
            patch.object(visual_qa, "changed_files", return_value=["src/app/views/home.html"]),
            patch.object(visual_qa, "run_check", side_effect=_playwright_unavailable),
        ):
            result = _run_visual_qa_gate(ticket, worktree)

        assert result is not None
        assert result["error"].startswith("[gate:visual_qa] DID NOT RUN")
        assert "playwright install chromium" in result["hint"]
        assert "--skip-visual-qa" in result["hint"]
        ticket.refresh_from_db()
        assert "Executable doesn't exist" in ticket.extra["visual_qa"]["not_run_reason"]

    def test_checkout_preparation_failure_refuses_before_server_start(self) -> None:
        ticket, worktree = self._ticket_and_worktree()
        overlay = _PageTargetOverlay()
        overlay.runtime = OverlayRuntime()
        overlay.runtime.visual_qa_server = MagicMock(return_value=RunCommand(args=["missing-server"]))
        with (
            patch("teatree.core.management.commands._ship.gates.get_overlay", return_value=overlay),
            patch.object(visual_qa, "changed_files", return_value=["page.html"]),
            patch.object(visual_qa, "run_check", return_value=[]),
            patch(
                "teatree.core.management.commands._ship.gates.ServiceLauncher.prepare",
                side_effect=RuntimeError("sync failed"),
            ) as prepare,
        ):
            result = _run_visual_qa_gate(ticket, worktree)

        assert result is not None
        assert result["error"].startswith("[gate:visual_qa] DID NOT RUN")
        assert "sync failed" in result["error"]
        prepare.assert_called_once_with(strict=True)

    def test_a_budget_that_ran_out_refuses_as_did_not_run(self) -> None:
        ticket, worktree = self._ticket_and_worktree()
        report = visual_qa.VisualQAReport(
            targets=["/a", "/b"], pages=[visual_qa.PageResult(url="http://x/a")], base_url="http://x"
        )
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", return_value=report),
        ):
            result = _run_visual_qa_gate(ticket, worktree)

        assert result is not None
        assert result["error"].startswith("[gate:visual_qa] DID NOT RUN")
        assert "/b" in result["error"]

    def test_a_no_target_rerun_replaces_a_failing_report(self) -> None:
        ticket, worktree = self._ticket_and_worktree()
        ticket.merge_extra(set_keys={"visual_qa": {"report_markdown": "## Visual QA\n\n:x: old failure"}})
        clean = visual_qa.VisualQAReport(targets=[], skipped_reason="no frontend changes")
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", return_value=clean),
            patch("teatree.core.management.commands._ship.gates.git.head_sha", return_value="a" * 40),
        ):
            assert _run_visual_qa_gate(ticket, worktree) is None

        ticket.refresh_from_db()
        assert ticket.extra["visual_qa"]["pages_checked"] == 0
        assert ":x:" not in ticket.extra["visual_qa"]["report_markdown"]

    def test_skip_with_unreadable_head_records_unavailable_head(self) -> None:
        ticket, worktree = self._ticket_and_worktree()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", return_value=visual_qa.VisualQAReport(targets=[])),
        ):
            assert _run_visual_qa_gate(ticket, worktree, skip_reason="reviewed") is None

        ticket.refresh_from_db()
        assert ticket.extra["visual_qa"]["bypass"]["head_sha"] == "unavailable"

    def test_skip_survives_server_hook_exception(self) -> None:
        ticket, worktree = self._ticket_and_worktree()
        overlay = _PageTargetOverlay()
        overlay.runtime = OverlayRuntime()
        overlay.runtime.visual_qa_server = MagicMock(side_effect=ValueError("broken hook"))
        with patch("teatree.core.management.commands._ship.gates.get_overlay", return_value=overlay):
            assert _run_visual_qa_gate(ticket, worktree, skip_reason="reviewed") is None

        ticket.refresh_from_db()
        assert "broken hook" in ticket.extra["visual_qa"]["not_run_reason"]

    def test_an_unreadable_diff_refuses_as_did_not_run(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/78")
        with tempfile.TemporaryDirectory() as not_a_repo:
            worktree = Worktree.objects.create(ticket=ticket, overlay="test", repo_path=not_a_repo, branch="feat-y")
            with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
                result = _run_visual_qa_gate(ticket, worktree)

        assert result is not None
        assert result["error"].startswith("[gate:visual_qa] DID NOT RUN")
        assert "git" in result["error"]

    def test_an_explicit_skip_records_unreadable_diff(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/79")
        with tempfile.TemporaryDirectory() as not_a_repo:
            worktree = Worktree.objects.create(ticket=ticket, overlay="test", repo_path=not_a_repo, branch="feat-z")
            with (
                patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
                patch("teatree.core.management.commands._ship.gates.git.head_sha", return_value="a" * 40),
            ):
                assert _run_visual_qa_gate(ticket, worktree, skip_reason="docs only") is None
        ticket.refresh_from_db()
        assert ticket.extra["visual_qa"]["bypass"]["reason"] == "docs only"

    def test_skip_still_records_browser_findings_and_head(self) -> None:
        ticket, worktree = self._ticket_and_worktree()
        finding = visual_qa.PageError(url="http://127.0.0.1:8000/", kind="http", message="HTTP 500")
        with (
            patch("teatree.core.management.commands._ship.gates.get_overlay", return_value=_PageTargetOverlay()),
            patch.object(visual_qa, "changed_files", return_value=["a.html"]),
            patch.object(
                visual_qa,
                "run_check",
                return_value=[visual_qa.PageResult(url="http://127.0.0.1:8000/", errors=[finding])],
            ),
            patch("teatree.core.management.commands._ship.gates.git.head_sha", return_value="b" * 40),
        ):
            assert _run_visual_qa_gate(ticket, worktree, skip_reason="owner approved") is None

        ticket.refresh_from_db()
        evidence = ticket.extra["visual_qa"]
        assert evidence["errors"] == 1
        assert evidence["bypass"]["reason"] == "owner approved"
        assert evidence["bypass"]["head_sha"] == "b" * 40
        assert evidence["bypass"]["at"]
        assert "owner approved" in evidence["report_markdown"]
        assert f"Bypass head: `{'b' * 40}`; at: {evidence['bypass']['at']}" in evidence["report_markdown"]

    def test_refused_connection_is_did_not_run_not_a_content_finding(self) -> None:
        ticket, worktree = self._ticket_and_worktree()
        report = visual_qa.VisualQAReport(
            targets=["/dash/board/"],
            pages=[visual_qa.PageResult(url="http://127.0.0.1:9/dash/board/", unreached=True)],
        )
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", return_value=report),
        ):
            result = _run_visual_qa_gate(ticket, worktree)

        assert result is not None
        assert result["error"].startswith("[gate:visual_qa] DID NOT RUN")

    def test_refused_connection_replayed_through_gate(self) -> None:
        ticket, worktree = self._ticket_and_worktree()
        context = MagicMock()
        page = context.new_page.return_value
        page.goto.side_effect = PlaywrightError("Page.goto: net::ERR_CONNECTION_REFUSED")
        page.locator.return_value.inner_text.return_value = ""
        with tempfile.TemporaryDirectory() as evidence_dir:

            def refused_page(targets: list[str], base_url: str) -> list[visual_qa.PageResult]:
                return [visual_qa._check_one(context, base_url, targets[0], Path(evidence_dir), 0)]

            with (
                patch("teatree.core.management.commands._ship.gates.get_overlay", return_value=_PageTargetOverlay()),
                patch.object(visual_qa, "changed_files", return_value=["a.html"]),
                patch.object(visual_qa, "run_check", side_effect=refused_page),
            ):
                result = _run_visual_qa_gate(ticket, worktree)

        assert result is not None
        assert result["error"].startswith("[gate:visual_qa] DID NOT RUN")
        assert "ERR_CONNECTION_REFUSED" in result["visual_qa"]["unchecked_reasons"]["/"]

    def test_a_server_that_died_during_the_run_is_did_not_run(self) -> None:
        ticket, worktree = self._ticket_and_worktree()
        report = visual_qa.VisualQAReport(
            targets=["/"],
            pages=[visual_qa.PageResult(url="http://127.0.0.1:9/")],
            base_url="http://127.0.0.1:9",
            server_failure="server exited 3: template crashed",
        )
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", return_value=report),
        ):
            result = _run_visual_qa_gate(ticket, worktree)

        assert result is not None
        assert result["error"].startswith("[gate:visual_qa] DID NOT RUN")
        assert "the checkout server failed (server exited 3: template crashed)" in result["error"]

    def test_server_hook_failure_is_did_not_run(self) -> None:
        ticket, worktree = self._ticket_and_worktree()
        overlay = MagicMock()
        overlay.runtime.visual_qa_server.side_effect = RuntimeError("server selection failed")

        with patch("teatree.core.management.commands._ship.gates.get_overlay", return_value=overlay):
            result = _run_visual_qa_gate(ticket, worktree)

        assert result is not None
        assert result["error"].startswith("[gate:visual_qa] DID NOT RUN")
        assert "server selection failed" in result["error"]

    def test_no_target_run_records_current_scope(self) -> None:
        ticket, worktree = self._ticket_and_worktree()
        clean = visual_qa.VisualQAReport(targets=[], skipped_reason="no frontend changes")
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", return_value=clean),
        ):
            assert _run_visual_qa_gate(ticket, worktree) is None

        ticket.refresh_from_db()
        assert ticket.extra["visual_qa"]["pages_checked"] == 0
        assert ticket.extra["visual_qa"]["repo_path"] == "/tmp/wt"
        assert ticket.extra["visual_qa"]["head_sha"] == "unavailable"

    def test_no_branch_server_is_did_not_run_at_the_gate(self) -> None:
        ticket, worktree = self._ticket_and_worktree()
        with (
            patch("teatree.core.management.commands._ship.gates.get_overlay", return_value=_PageTargetOverlay()),
            patch.object(visual_qa, "changed_files", return_value=["a.html"]),
            patch("teatree.core.management.commands._e2e_resolvers._disc.compose_frontend_port", return_value=None),
        ):
            result = _run_visual_qa_gate(ticket, worktree)

        assert result is not None
        assert result["error"].startswith("[gate:visual_qa] DID NOT RUN")
        assert "no server for this branch" in result["error"]

    def test_records_summary_when_pages_checked(self) -> None:
        ticket, worktree = self._ticket_and_worktree()
        page = visual_qa.PageResult(url="http://x/", screenshot_path=".t3/visual_qa/00-root.png")
        report = visual_qa.VisualQAReport(targets=["/"], pages=[page], base_url="http://x")
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", return_value=report),
        ):
            assert _run_visual_qa_gate(ticket, worktree) is None

        ticket.refresh_from_db()
        assert ticket.extra["visual_qa"]["pages_checked"] == 1
        assert ticket.extra["visual_qa"]["errors"] == 0

    def test_returns_error_when_findings(self) -> None:
        ticket, worktree = self._ticket_and_worktree()
        page = visual_qa.PageResult(
            url="http://x/",
            errors=[visual_qa.PageError(url="http://x/", kind="page", message="boom")],
        )
        report = visual_qa.VisualQAReport(targets=["/"], pages=[page], base_url="http://x")
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", return_value=report),
        ):
            result = _run_visual_qa_gate(ticket, worktree)

        assert result is not None
        assert result["allowed"] is False
        assert "1 blocking finding" in result["error"]
        assert "## Visual QA" in result["report_markdown"]

        ticket.refresh_from_db()
        assert ticket.extra["visual_qa"]["errors"] == 1

    def test_scans_the_worktree_it_is_given_not_the_tickets_earliest(self) -> None:
        """#776 N1: visual QA inspects the invoking workstream's repo.

        The gate no longer re-resolves — it scans the row ``pr create`` resolved
        and is about to ship, so a ticket whose EARLIEST row is a stale
        already-merged workstream is never the one scanned. Which row that is
        stays pinned end-to-end by ``test_create_invoking_cwd``.
        """
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/776")
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="/tmp/repo-pr-a", branch="s-776-pr-a-merged")
        invoking = Worktree.objects.create(
            ticket=ticket, overlay="test", repo_path="/tmp/repo-pr-b", branch="s-776-pr-b-current"
        )
        captured: dict[str, str] = {}

        def fake_changed_files(*, repo: str) -> list[str]:
            captured["repo"] = repo
            return []

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "changed_files", side_effect=fake_changed_files),
        ):
            assert _run_visual_qa_gate(ticket, invoking) is None

        # The invoking PR-B repo is scanned — NOT the stale PR-A first() row.
        assert captured["repo"] == "/tmp/repo-pr-b"

    @staticmethod
    def _repo_given_to_changed_files(ticket: Ticket, worktree: Worktree) -> str:
        captured: dict[str, str] = {}

        def fake_changed_files(*, repo: str) -> list[str]:
            captured["repo"] = repo
            return []

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "changed_files", side_effect=fake_changed_files),
        ):
            assert _run_visual_qa_gate(ticket, worktree) is None
        return captured["repo"]

    def test_reads_the_diff_from_the_checkout_directory_not_the_repo_slug(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/5145")
        with tempfile.TemporaryDirectory() as checkout:
            worktree = Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="souliane/teatree",
                branch="feat-slug",
                extra={"worktree_path": checkout},
            )

            assert self._repo_given_to_changed_files(ticket, worktree) == checkout

    def test_reads_the_diff_from_repo_path_when_no_checkout_directory_is_recorded(self) -> None:
        ticket, worktree = self._ticket_and_worktree()

        assert self._repo_given_to_changed_files(ticket, worktree) == "/tmp/wt"

    def test_records_checkout_head_and_path_in_visual_qa_summary(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/5537")
        with tempfile.TemporaryDirectory() as checkout:
            repo = make_git_repo(Path(checkout))
            head = run_git(repo, "rev-parse", "HEAD")
            worktree = Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="souliane/teatree",
                branch="feat-slug",
                extra={"worktree_path": checkout},
            )

            assert self._repo_given_to_changed_files(ticket, worktree) == checkout
            ticket.refresh_from_db()
            assert ticket.extra["visual_qa"]["head_sha"] == head
            assert ticket.extra["visual_qa"]["repo_path"] == str(Path(checkout).resolve())

    def test_skip_reason_propagates(self) -> None:
        ticket, worktree = self._ticket_and_worktree()
        captured: dict[str, str] = {}

        def fake_evaluate(**kwargs: object) -> visual_qa.VisualQAReport:
            captured["skip_reason"] = str(kwargs.get("skip_reason", ""))
            return visual_qa.VisualQAReport(targets=[], skipped_reason="--skip: my reason")

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", side_effect=fake_evaluate),
            patch("teatree.core.management.commands._ship.gates.git.head_sha", return_value="a" * 40),
        ):
            assert _run_visual_qa_gate(ticket, worktree, skip_reason="my reason") is None

        assert captured["skip_reason"] == "my reason"
        ticket.refresh_from_db()
        assert ticket.extra["visual_qa"]["bypass"]["head_sha"] == "a" * 40


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

    def test_commits_ahead_but_no_content_over_base_blocks(self) -> None:
        """#4429: a squash-merged branch that merged the base back would ship an empty PR."""
        with tempfile.TemporaryDirectory() as root:
            self._git(root, "init", "-q", "-b", "main")
            self._git(root, "config", "user.email", "t@example.com")
            self._git(root, "config", "user.name", "t")
            (Path(root) / "a").write_text("1", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-q", "-m", "base")
            self._git(root, "checkout", "-q", "-b", "feat-squashed")
            (Path(root) / "b").write_text("2", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-q", "-m", "feat: the work")
            self._git(root, "checkout", "-q", "main")
            self._git(root, "merge", "-q", "--squash", "feat-squashed")
            self._git(root, "commit", "-q", "-m", "feat: the work (#4422)")
            self._git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
            self._git(root, "checkout", "-q", "feat-squashed")
            self._git(root, "merge", "-q", "--no-edit", "origin/main")

            result = _assert_commits_ahead_of_base(self._wt(root, "feat-squashed"))

        assert result is not None, "a branch whose pull request would be empty MUST block"
        assert result["branch"] == "feat-squashed"
        assert "carries no changes" in result["error"]
        # #4551: a three-dot probe measures an empty delta, never its cause.
        assert "retry `pr create`" in result["error"], "the refusal must name a remedy, not only a cause"
        assert "squash-merge" not in result["error"]

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
        naming the branch/repo so a mistaken PR_OPENED whose hollowness could not
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
