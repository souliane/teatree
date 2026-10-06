"""``t3 browser`` against a real headless Chromium: what a page did is reported, step after step.

Outside ``testpaths=[tests]``: CI's browser job runs it with
``uv run pytest e2e/browser -n0 -p no:randomly -p no:cacheprovider``.
"""

from http import HTTPStatus
from pathlib import Path

from e2e.browser.pom import BrowserCli, require_findings, require_launchable_browser
from e2e.browser.site import BrokenSite
from teatree.visual_qa import run_check


def test_open_reports_the_console_error_and_the_failed_request(site: BrokenSite, browser: BrowserCli) -> None:
    report = browser.step("open", site.broken_url)

    report.require_event(kind="console", level="error", text="diag-boom")
    report.require_event(kind="requestfailed", url=site.refused_url, text="net::ERR_CONNECTION_REFUSED")
    report.require_event(kind="response", url=site.not_found_url, status=HTTPStatus.NOT_FOUND)


def test_open_on_a_clean_page_reports_no_errors(site: BrokenSite, browser: BrowserCli) -> None:
    report = browser.step("open", site.clean_url)

    report.require_event(kind="response", url=site.clean_url, status=HTTPStatus.OK)
    report.require_no_findings()


def test_steps_reuse_one_headless_browser_until_close(site: BrokenSite, browser: BrowserCli) -> None:
    browser.step("open", site.broken_url)
    keeper = browser.keeper_pid()
    browser.step("act", "click", "text=Go")
    inspection = browser.step("inspect")

    browser.require_one_headless_keeper(keeper)
    inspection.require_console("diag-boom", "diag-clicked")
    inspection.require_snapshot_line('button "Go"')
    inspection.require_png_screenshot()
    browser.close()
    browser.require_keeper_gone(keeper)
    browser.refused("inspect", naming="no open browser session")


def test_the_doctor_probe_launches_the_installed_headless_browser() -> None:
    require_launchable_browser()


def test_the_visual_qa_gate_reports_the_page_through_the_same_recorder(site: BrokenSite, tmp_path: Path) -> None:
    [page] = run_check(["/broken"], site.origin, screenshot_dir=str(tmp_path))

    require_findings(
        {(error.kind, error.message) for error in page.errors},
        ("console", "diag-boom"),
        ("http", f"HTTP 404: {site.not_found_url}"),
    )
