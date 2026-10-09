"""``t3 browser`` against a real headless Chromium: what a page did is reported, step after step.

Outside ``testpaths=[tests]``: CI's browser job runs it with
``uv run pytest e2e/browser -n0 -p no:randomly -p no:cacheprovider``.
"""

from http import HTTPStatus
from pathlib import Path

from e2e.browser.pom import BrowserCli, require_findings, require_launchable_browser
from e2e.browser.site import BrokenSite
from teatree.visual_qa import run_check

KEEPER_EXIT_WAIT_S = 15.0


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
    browser.step("open", site.clean_url)

    browser.require_one_headless_keeper(keeper)
    browser.require_owner_only_session_files()
    inspection.require_console("diag-boom", "diag-clicked")
    inspection.require_snapshot_line('button "Go"')
    inspection.require_png_screenshot()
    browser.close()
    browser.require_keeper_gone(keeper)
    browser.refused("inspect", naming="no open browser session")


def test_inspect_reports_the_http_error_of_the_loaded_document_itself(site: BrokenSite, browser: BrowserCli) -> None:
    browser.step("open", site.clean_url)
    browser.step("open", site.down_url)

    inspection = browser.step("inspect")

    inspection.require_event(kind="response", url=site.down_url, status=HTTPStatus.INTERNAL_SERVER_ERROR)
    inspection.require_no_event(url=site.clean_url)


def test_a_timed_out_open_still_reports_what_the_page_did(site: BrokenSite, browser: BrowserCli) -> None:
    report = browser.failed_step("open", site.stalled_url, "--timeout", "2", naming="Timeout 2000ms exceeded")

    report.require_event(kind="console", level="error", text="diag-stalled")


def test_sigterm_ends_the_keeper_and_the_next_open_starts_a_new_one(site: BrokenSite, browser: BrowserCli) -> None:
    browser.step("open", site.clean_url)

    ended = browser.end_keeper_with_sigterm()

    browser.require_keeper_gone(ended, within_s=KEEPER_EXIT_WAIT_S)
    browser.refused("inspect", naming="no open browser session")
    browser.step("open", site.clean_url).require_no_findings()
    browser.require_a_keeper_other_than(ended)


def test_an_idle_session_ends_its_own_keeper(site: BrokenSite, browser: BrowserCli) -> None:
    browser.step("open", site.clean_url)

    idle = browser.let_the_session_go_idle()

    browser.require_keeper_gone(idle, within_s=KEEPER_EXIT_WAIT_S)
    browser.refused("inspect", naming="no open browser session")


def test_removing_the_session_directory_ends_the_keeper_and_leaves_nothing(
    site: BrokenSite, browser: BrowserCli
) -> None:
    browser.step("open", site.broken_url)

    torn_down = browser.remove_session_directory()

    browser.require_keeper_gone(torn_down, within_s=KEEPER_EXIT_WAIT_S)
    browser.require_no_session_directory()


def test_the_doctor_probe_launches_the_installed_headless_browser() -> None:
    require_launchable_browser()


def test_the_visual_qa_gate_reports_the_page_through_the_same_recorder(site: BrokenSite, tmp_path: Path) -> None:
    [page] = run_check(["/broken"], site.origin, screenshot_dir=str(tmp_path))

    require_findings(
        {(error.kind, error.message) for error in page.errors},
        ("console", "diag-boom"),
        ("http", f"HTTP 404: {site.not_found_url}"),
    )
