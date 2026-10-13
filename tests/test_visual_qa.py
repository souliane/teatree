"""Unit tests for the pre-push browser sanity gate."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import Error as PlaywrightError

from teatree import visual_qa
from teatree.browser.evidence import BrowserEvent
from teatree.types import RunCommand
from teatree.utils.run import CommandFailedError
from tests._git_repo import make_git_repo, run_git


def _unreadable_diff() -> list[str]:
    raise CommandFailedError(["git", "diff"], 1, "", "the diff could not be read")


class TestMatchesTriggers:
    def test_html_template_matches(self) -> None:
        assert visual_qa.matches_triggers(["src/teatree/templates/dashboard.html"]) == [
            "src/teatree/templates/dashboard.html",
        ]

    def test_python_only_does_not_match(self) -> None:
        assert visual_qa.matches_triggers(["src/teatree/visual_qa.py"]) == []

    def test_translation_json_matches(self) -> None:
        assert visual_qa.matches_triggers(["frontend/src/i18n/en.json"]) == [
            "frontend/src/i18n/en.json",
        ]

    def test_custom_globs(self) -> None:
        assert visual_qa.matches_triggers(["docs/notes.md"], ("*.md",)) == ["docs/notes.md"]


class TestDetectTargets:
    def test_returns_root_when_default_triggers_match(self) -> None:
        assert visual_qa.detect_targets(["src/teatree/templates/dashboard.html"]) == ["/"]

    def test_returns_empty_when_no_triggers_match(self) -> None:
        assert visual_qa.detect_targets(["src/teatree/visual_qa.py"]) == []

    def test_overlay_overrides_default(self) -> None:
        overlay = MagicMock()
        overlay.review.visual_qa_targets.return_value = ["/dashboard/", "/admin/"]
        assert visual_qa.detect_targets(["a.html"], overlay) == ["/dashboard/", "/admin/"]

    def test_overlay_returns_empty_skips(self) -> None:
        overlay = MagicMock()
        overlay.review.visual_qa_targets.return_value = []
        # Even when default triggers would match, the overlay's empty list wins.
        assert visual_qa.detect_targets(["a.html"], overlay) == []

    def test_overlay_targets_past_page_cap_are_reported(self) -> None:
        overlay = MagicMock()
        overlay.review.visual_qa_targets.return_value = [f"/page-{i}/" for i in range(visual_qa.MAX_PAGES + 2)]
        result = visual_qa.detect_targets(["a.html"], overlay)
        assert len(result) == visual_qa.MAX_PAGES + 2


class TestEvaluate:
    def test_browser_is_checked_before_checkout_server_preparation(self, tmp_path: Path, monkeypatch) -> None:
        prepared = MagicMock()
        spec = visual_qa.ServerSpec(RunCommand(args=["missing"], cwd=tmp_path), str(tmp_path), prepared)

        def missing_browser(targets: list[str], base_url: str) -> list[visual_qa.PageResult]:
            message = "browser unavailable"
            raise visual_qa.VisualQAUnavailableError(message)

        monkeypatch.setattr(visual_qa, "run_check", missing_browser)
        report = visual_qa.evaluate(read_diff=lambda: ["a.html"], overlay=None, base_url="", server=spec)

        assert report.not_run_reason == "browser unavailable"
        prepared.assert_not_called()

    def test_skip_records_a_missing_docker_cli(self) -> None:
        def missing_docker() -> str:
            message = "docker"
            raise FileNotFoundError(message)

        report = visual_qa.evaluate(
            read_diff=lambda: ["a.html"], overlay=None, base_url=missing_docker, skip_reason="reviewed"
        )

        assert report.skipped_reason == "--skip: reviewed"
        assert "FileNotFoundError" in report.not_run_reason

    def test_over_cap_targets_fail_before_browser_start(self, monkeypatch) -> None:
        overlay = MagicMock()
        overlay.review.visual_qa_targets.return_value = [f"/p{i}/" for i in range(visual_qa.MAX_PAGES + 1)]
        checked = MagicMock()
        monkeypatch.setattr(visual_qa, "run_check", checked)

        report = visual_qa.evaluate(read_diff=lambda: ["a.html"], overlay=overlay, base_url="http://x")

        assert "too many visual QA targets" in report.not_run_reason
        assert len(report.targets) == visual_qa.MAX_PAGES + 1
        checked.assert_not_called()

    def test_skip_reason_still_checks_pages(self, monkeypatch) -> None:
        checked: list[str] = []
        monkeypatch.setattr(visual_qa, "run_check", lambda targets, base_url: checked.extend(targets) or [])
        report = visual_qa.evaluate(
            read_diff=lambda: ["a.html"], overlay=None, base_url="http://x", skip_reason="not relevant"
        )
        assert report.skipped_reason == "--skip: not relevant"
        assert checked == ["/"]

    def test_legacy_env_variable_cannot_disable_the_check(self, monkeypatch) -> None:
        checked: list[str] = []
        monkeypatch.setenv("T3_VISUAL_QA", "disabled")
        monkeypatch.setattr(visual_qa, "run_check", lambda targets, base_url: checked.extend(targets) or [])

        visual_qa.evaluate(read_diff=lambda: ["a.html"], overlay=None, base_url="http://x")

        assert checked == ["/"]

    def test_no_targets_skipped(self) -> None:
        report = visual_qa.evaluate(read_diff=lambda: ["a.py"], overlay=None, base_url="http://x")
        assert report.skipped_reason == "no frontend changes"
        assert report.pages == []

    def test_explicit_skip_records_unreadable_diff(self) -> None:
        report = visual_qa.evaluate(
            read_diff=_unreadable_diff, overlay=None, base_url="http://x", skip_reason="reviewed"
        )
        assert report.not_run_reason

    def test_playwright_unavailable_did_not_run(self, monkeypatch) -> None:
        message = "browser missing"

        def _raise(*args: object, **kwargs: object) -> object:
            raise visual_qa.VisualQAUnavailableError(message)

        monkeypatch.setattr(visual_qa, "run_check", _raise)
        report = visual_qa.evaluate(read_diff=lambda: ["a.html"], overlay=None, base_url="http://x")
        assert report.not_run_reason == message
        assert report.skipped_reason == ""
        assert report.targets == ["/"]
        assert report.has_errors
        assert f"did not run: {message}" in visual_qa.format_report(report)


class TestRunCheckWithoutPlaywright:
    def test_a_missing_playwright_is_the_handled_did_not_run(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

        report = visual_qa.evaluate(read_diff=lambda: ["a.html"], overlay=None, base_url="http://127.0.0.1:9")

        assert report.not_run_reason.startswith("Playwright is not importable in the environment running `t3`")
        assert "t3 doctor check --repair" in report.not_run_reason
        assert report.has_errors


class TestServedApp:
    def test_server_is_sandboxed_and_process_group_is_reaped(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("T3_CONFIG_DB", "/tmp/live-config.sqlite3")
        script = (
            "import os,socket,time;"
            "print(os.environ.get('T3_CONFIG_DB','absent'),flush=True);"
            "s=socket.socket();s.bind(('127.0.0.1',int(os.environ['T3_VISUAL_QA_PORT'])));"
            "s.listen();time.sleep(30)"
        )
        spec = visual_qa.ServerSpec(
            RunCommand(args=[sys.executable, "-c", script], cwd=tmp_path), str(tmp_path), lambda: None
        )

        with visual_qa._served_app(spec) as app:
            pgid = app.pgid
            process = app.process
            assert "absent" in app.log_path.read_text()
            assert app.base_url.startswith("http://127.0.0.1:")

        assert process.poll() is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(pgid, 0)

    def test_missing_checkout_script_is_did_not_run(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(visual_qa, "run_check", lambda targets, base_url: [])
        spec = visual_qa.ServerSpec(
            RunCommand(args=[sys.executable, "missing-serve.py"], cwd=tmp_path), str(tmp_path), lambda: None
        )

        report = visual_qa.evaluate(read_diff=lambda: ["page.html"], overlay=None, base_url="", server=spec)

        assert "server exited" in report.not_run_reason
        assert "missing-serve.py" in report.not_run_reason
        assert report.unchecked_reasons["/"].startswith("unreached")


class TestChangedFilesReadsTheResolvedBase:
    def test_an_unresolvable_base_raises_rather_than_reading_no_changes(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("T3_DIFF_COVERAGE_BASE", raising=False)
        repo = make_git_repo(tmp_path / "repo")

        with pytest.raises(CommandFailedError):
            visual_qa.changed_files(repo=str(repo))

    def test_the_configured_diff_base_is_honoured(self, tmp_path: Path, monkeypatch) -> None:
        repo = make_git_repo(tmp_path / "repo")
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "page.html").write_text("<p>page</p>")
        run_git(repo, "add", "page.html")
        run_git(repo, "commit", "-q", "-m", "page")
        monkeypatch.setenv("T3_DIFF_COVERAGE_BASE", "refs/heads/main")

        assert visual_qa.changed_files(repo=str(repo)) == ["page.html"]


class TestRunCheckLaunchesHeadless:
    """The pre-push visual-QA gate must never pop a visible window on a headless box."""

    def test_chromium_launched_headless(self, monkeypatch, tmp_path) -> None:
        launcher = MagicMock()
        pw = MagicMock()
        pw.chromium.launch = launcher
        context_manager = MagicMock()
        context_manager.__enter__.return_value = pw
        monkeypatch.setattr(
            "playwright.sync_api.sync_playwright",
            lambda: context_manager,
            raising=True,
        )

        visual_qa.run_check([], base_url="http://x", screenshot_dir=str(tmp_path))

        assert launcher.call_args.kwargs.get("headless") is True


class TestFindingRules:
    """The gate's findings: console errors, page errors, and HTTP errors other than 401/403."""

    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            (BrowserEvent("console", text="boom", level="error"), ("console", "boom")),
            (BrowserEvent("pageerror", text="TypeError: x"), ("page", "TypeError: x")),
            (BrowserEvent("response", url="http://h/a.js", status=404), ("http", "HTTP 404: http://h/a.js")),
            (BrowserEvent("response", url="http://h/api", status=500), ("http", "HTTP 500: http://h/api")),
        ],
    )
    def test_a_finding_keeps_its_kind_and_message(self, event: BrowserEvent, expected: tuple[str, str]) -> None:
        error = visual_qa._page_error("http://h/", event)

        assert error is not None
        assert (error.kind, error.message) == expected

    @pytest.mark.parametrize(
        "event",
        [
            BrowserEvent("console", text="hello", level="warning"),
            BrowserEvent("response", url="http://h/me", status=401),
            BrowserEvent("response", url="http://h/admin", status=403),
            BrowserEvent("response", url="http://h/", status=200),
            BrowserEvent("requestfailed", text="net::ERR_ABORTED", url="http://h/x", method="GET"),
            BrowserEvent("navigation", url="http://h/"),
        ],
    )
    def test_everything_else_is_not_a_finding(self, event: BrowserEvent) -> None:
        assert visual_qa._page_error("http://h/", event) is None


class TestNavigationClassification:
    @pytest.mark.parametrize(
        ("failure", "unreached"),
        [
            ("net::ERR_CONNECTION_REFUSED", True),
            ("net::ERR_NAME_NOT_RESOLVED", True),
            ("net::ERR_ADDRESS_UNREACHABLE", True),
            ("net::ERR_CONNECTION_RESET", False),
            ("net::ERR_EMPTY_RESPONSE", False),
        ],
    )
    def test_only_never_connected_errors_are_unreached(self, failure: str, tmp_path: Path, *, unreached: bool) -> None:
        context = MagicMock()
        page = context.new_page.return_value
        page.goto.side_effect = PlaywrightError(f"Page.goto: {failure}")
        page.locator.return_value.inner_text.return_value = ""

        result = visual_qa._check_one(context, "http://127.0.0.1:9", "/dash/board/", tmp_path, 0)

        assert result.unreached is unreached
        assert bool(result.errors) is not unreached


class TestVisualQAReport:
    def test_deadline_covers_the_full_page_budget(self) -> None:
        assert visual_qa.TOTAL_TIMEOUT_S >= visual_qa.MAX_PAGES * visual_qa.PER_PAGE_TIMEOUT_MS / 1000

    def test_report_has_no_loopback_or_broken_local_screenshot(self) -> None:
        report = visual_qa.VisualQAReport(
            targets=["/dash/board/"],
            pages=[
                visual_qa.PageResult(url="http://127.0.0.1:1234/dash/board/", screenshot_path=".t3/visual_qa/x.png")
            ],
            base_url="http://127.0.0.1:1234",
        )

        markdown = visual_qa.format_report(report)

        assert "http://127.0.0.1" not in markdown
        assert "![" not in markdown
        assert "`/dash/board/`" in markdown

    def test_unchecked_reason_does_not_copy_multiline_browser_banner(self) -> None:
        report = visual_qa.VisualQAReport(
            targets=["/a", "/b"], not_run_reason="browser missing\ninstall banner\nmore detail"
        )

        assert report.unchecked_reasons == {"/a": "unreached", "/b": "unreached"}
        assert visual_qa.format_report(report).count("install banner") == 1

    def test_unreached_connection_is_unknown_even_without_findings(self) -> None:
        report = visual_qa.VisualQAReport(
            targets=["/dash/board/"],
            pages=[visual_qa.PageResult(url="http://127.0.0.1:9/dash/board/", unreached=True)],
        )
        assert report.unchecked == ["/dash/board/"]
        assert report.unchecked_reasons["/dash/board/"] == "unreached"
        assert report.total_errors == 0
        assert report.pages_checked == 0

    def test_page_cap_is_named_for_every_later_target(self) -> None:
        targets = [f"/p{i}" for i in range(visual_qa.MAX_PAGES + 2)]
        report = visual_qa.VisualQAReport(
            targets=targets, pages=[visual_qa.PageResult(url=p) for p in targets[: visual_qa.MAX_PAGES]]
        )
        assert report.unchecked_reasons == {
            f"/p{visual_qa.MAX_PAGES}": "page cap",
            f"/p{visual_qa.MAX_PAGES + 1}": "page cap",
        }

    def test_a_report_with_no_targets_has_no_errors(self) -> None:
        report = visual_qa.VisualQAReport(targets=[])
        assert report.has_errors is False
        assert report.total_errors == 0

    def test_a_target_the_run_never_reached_is_an_error(self) -> None:
        # The 60s budget cuts the loop mid-list; a page nobody loaded is not a clean page.
        page = visual_qa.PageResult(url="http://x/a")
        report = visual_qa.VisualQAReport(targets=["/a", "/b"], pages=[page], base_url="http://x")
        assert report.unchecked == ["/b"]
        assert report.has_errors is True
        assert "Not checked" in visual_qa.format_report(report)

    def test_a_skipped_report_is_not_reported_incomplete(self) -> None:
        report = visual_qa.VisualQAReport(targets=[], skipped_reason="--skip: docs only")
        assert report.unchecked == []
        assert report.has_errors is False

    def test_errors_aggregated(self) -> None:
        page = visual_qa.PageResult(
            url="http://x/",
            errors=[
                visual_qa.PageError(url="http://x/", kind="page", message="boom"),
                visual_qa.PageError(url="http://x/", kind="console", message="warn"),
            ],
        )
        report = visual_qa.VisualQAReport(targets=["/"], pages=[page])
        assert report.has_errors is True
        assert report.total_errors == 2

    def test_summary_serialises(self) -> None:
        page = visual_qa.PageResult(
            url="http://x/",
            errors=[visual_qa.PageError(url="http://x/", kind="page", message="boom")],
        )
        report = visual_qa.VisualQAReport(targets=["/"], pages=[page], base_url="http://x")
        summary = report.summary()
        assert summary["pages_checked"] == 1
        assert summary["errors"] == 1
        details = summary["details"]
        assert isinstance(details, list)
        first_detail = details[0]
        assert isinstance(first_detail, dict)
        assert first_detail["url"] == "http://x/"


class TestFormatReport:
    def test_skipped_section(self) -> None:
        report = visual_qa.VisualQAReport(targets=[], skipped_reason="--skip: docs only")
        out = visual_qa.format_report(report)
        assert "## Visual QA" in out
        assert "docs only" in out

    def test_no_targets_section(self) -> None:
        report = visual_qa.VisualQAReport(targets=[])
        out = visual_qa.format_report(report)
        assert "no frontend changes detected" in out

    def test_clean_run_renders_check(self) -> None:
        page = visual_qa.PageResult(url="http://x/", screenshot_path=".t3/visual_qa/00-root.png")
        report = visual_qa.VisualQAReport(targets=["/"], pages=[page], base_url="http://x")
        out = visual_qa.format_report(report)
        assert ":white_check_mark:" in out
        assert "0 finding" in out
        assert ".t3/visual_qa/00-root.png" not in out

    def test_findings_render_x_and_kinds(self) -> None:
        page = visual_qa.PageResult(
            url="http://x/dashboard/",
            errors=[
                visual_qa.PageError(url="http://x/dashboard/", kind="translation", message="raw key in DOM: app.x.y"),
                visual_qa.PageError(url="http://x/dashboard/", kind="http", message="HTTP 500: /api/foo"),
            ],
        )
        report = visual_qa.VisualQAReport(targets=["/dashboard/"], pages=[page], base_url="http://x")
        out = visual_qa.format_report(report)
        assert ":x:" in out
        assert "**translation**" in out
        assert "**http**" in out


class TestSlug:
    def test_root_path(self) -> None:
        assert visual_qa._slug("/", 0) == "00-root"

    def test_nested_path(self) -> None:
        assert visual_qa._slug("/dashboard/foo/", 3) == "03-dashboard-foo"

    def test_special_chars_stripped(self) -> None:
        assert visual_qa._slug("/users/?id=42", 1) == "01-users-id-42"
