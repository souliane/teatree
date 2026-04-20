"""Unit tests for the pre-push browser sanity gate."""

from unittest.mock import MagicMock

from teatree import visual_qa


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
        overlay.get_visual_qa_targets.return_value = ["/dashboard/", "/admin/"]
        assert visual_qa.detect_targets(["a.html"], overlay) == ["/dashboard/", "/admin/"]

    def test_overlay_returns_empty_skips(self) -> None:
        overlay = MagicMock()
        overlay.get_visual_qa_targets.return_value = []
        # Even when default triggers would match, the overlay's empty list wins.
        assert visual_qa.detect_targets(["a.html"], overlay) == []

    def test_overlay_targets_capped_at_max_pages(self) -> None:
        overlay = MagicMock()
        overlay.get_visual_qa_targets.return_value = [f"/page-{i}/" for i in range(10)]
        result = visual_qa.detect_targets(["a.html"], overlay)
        assert len(result) == visual_qa.MAX_PAGES


class TestShouldRun:
    def test_runs_by_default(self) -> None:
        assert visual_qa.should_run(env={}) == (True, "")

    def test_skip_reason_blocks(self) -> None:
        run, reason = visual_qa.should_run(skip_reason="ticket comment-only", env={})
        assert run is False
        assert "ticket comment-only" in reason

    def test_env_disabled_blocks(self) -> None:
        run, reason = visual_qa.should_run(env={"T3_VISUAL_QA": "disabled"})
        assert run is False
        assert "T3_VISUAL_QA=disabled" in reason

    def test_env_disabled_case_insensitive(self) -> None:
        run, _ = visual_qa.should_run(env={"T3_VISUAL_QA": "DISABLED"})
        assert run is False

    def test_env_enabled_runs(self) -> None:
        assert visual_qa.should_run(env={"T3_VISUAL_QA": "enabled"}) == (True, "")


class TestEvaluate:
    def test_skipped_reason_returned(self) -> None:
        report = visual_qa.evaluate(diff=["a.html"], overlay=None, base_url="http://x", skip_reason="not relevant")
        assert report.skipped_reason == "--skip: not relevant"
        assert report.pages == []

    def test_no_targets_skipped(self) -> None:
        report = visual_qa.evaluate(diff=["a.py"], overlay=None, base_url="http://x")
        assert report.skipped_reason == "no frontend changes"
        assert report.pages == []

    def test_env_disabled_skipped(self) -> None:
        report = visual_qa.evaluate(
            diff=["a.html"],
            overlay=None,
            base_url="http://x",
            env={"T3_VISUAL_QA": "disabled"},
        )
        assert "T3_VISUAL_QA=disabled" in report.skipped_reason

    def test_playwright_unavailable_returns_report(self, monkeypatch) -> None:
        message = "browser missing"

        def _raise(*args: object, **kwargs: object) -> object:
            raise visual_qa.VisualQAUnavailableError(message)

        monkeypatch.setattr(visual_qa, "run_check", _raise)
        report = visual_qa.evaluate(diff=["a.html"], overlay=None, base_url="http://x")
        assert report.skipped_reason == message
        assert report.targets == ["/"]
        assert not report.has_errors


class TestVisualQAReport:
    def test_empty_report_has_no_errors(self) -> None:
        report = visual_qa.VisualQAReport(targets=["/"])
        assert report.has_errors is False
        assert report.total_errors == 0

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
        report = visual_qa.VisualQAReport(targets=[], skipped_reason="T3_VISUAL_QA=disabled")
        out = visual_qa.format_report(report)
        assert "## Visual QA" in out
        assert "T3_VISUAL_QA=disabled" in out

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
        assert ".t3/visual_qa/00-root.png" in out

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
