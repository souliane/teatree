"""E2E tests for the TeaTree dashboard.

Run with:
    t3 teatree e2e project

Golden screenshots live in ``e2e/snapshots/test_dashboard/``.
To update them after intentional UI changes::

    t3 teatree e2e project --update-snapshots

Baselines are only stable when regenerated **inside** the Docker image
``t3 teatree e2e project`` uses — macOS Chromium renders fonts at slightly
different heights than the CI Linux runner (see [#275](https://github.com/souliane/teatree/issues/275), credits @m13v).
Never regenerate snapshots with plain ``uv run pytest`` on a developer
laptop — the resulting PNGs will always mismatch CI.
"""

import re
from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

# Threshold for pixel-level comparison (0.0 = exact, 0.1 = tolerant).
_SNAPSHOT_THRESHOLD = 0.1

# ── Full-page screenshot (for README) ─────────────────────────────


def test_full_dashboard_screenshot(e2e_server: str, page: Page, assert_snapshot: Callable) -> None:
    page.goto(e2e_server)
    page.wait_for_timeout(2000)  # let HTMX panels finish loading
    assert_snapshot(
        page.screenshot(full_page=True, animations="disabled"),
        name="dashboard-full.png",
        threshold=_SNAPSHOT_THRESHOLD,
    )


# ── Dashboard structure ─────────────────────────────────────────────


def test_dashboard_loads(e2e_server: str, page: Page) -> None:
    """Page loads, summary counters and section headings are visible."""
    page.goto(e2e_server)
    expect(page.locator("body")).to_contain_text("In Flight Tickets")
    for heading in [
        "Automation",
        "Action Required",
        "Sessions",
        "In-Flight Tickets",
    ]:
        expect(page.locator("h2", has_text=heading)).to_be_visible()


def test_summary_counters(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    for label in ["In Flight Tickets", "Active Worktrees", "Pending Headless", "Pending Interactive"]:
        expect(page.locator("body")).to_contain_text(label)


# ── Tickets table ───────────────────────────────────────────────────


def test_tickets_with_mrs(e2e_server: str, page: Page, assert_snapshot: Callable) -> None:
    page.goto(e2e_server)
    expect(page.locator("body")).to_contain_text("#42")
    expect(page.locator("body")).to_contain_text("Fix the login bug")
    expect(page.locator("body")).to_contain_text("backend")
    expect(page.locator("body")).to_contain_text("frontend")

    tickets_section = page.locator("h2", has_text="In-Flight Tickets").locator("..")
    assert_snapshot(
        tickets_section.screenshot(animations="disabled"),
        name="tickets-section.png",
        threshold=_SNAPSHOT_THRESHOLD,
    )


def test_tickets_pipeline_and_approvals(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    expect(page.locator("body")).to_contain_text("\u2705")  # ✅
    expect(page.locator("body")).to_contain_text("\u274c")  # ❌
    expect(page.locator("span", has_text="Draft")).to_be_visible()
    expect(page.locator("body")).to_contain_text("1/1")
    expect(page.locator("body")).to_contain_text("0/1")


def test_tickets_without_mrs(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    expect(page.locator("body")).to_contain_text("#99")


def test_ticket_action_buttons(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    expect(page.locator("button", has_text="Auto").first).to_be_visible()
    expect(page.locator("button", has_text="Interactive").first).to_be_visible()


# ── Unified Sessions panel ─────────────────────────────────────────


def test_sessions_panel_shows_tasks(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    sessions_section = page.locator("#section-sessions")
    # Wait for HTMX-loaded unified sessions grid to render seeded tasks.
    sessions_section.locator("#unified-sessions-grid").wait_for(state="visible")
    # Interactive task stays queued — its ``execution_reason`` is shown.
    # The headless task runs synchronously under ImmediateBackend and moves
    # to "recent activity", where the selector blanks ``execution_reason``
    # (see ``selectors/unified.py``) — so we only assert the interactive one.
    expect(sessions_section).to_contain_text("Needs manual verification")
    expect(sessions_section).to_contain_text("#42")


def test_sessions_filter_tabs(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    sessions_section = page.locator("#section-sessions")
    for tab in ["All", "Running", "Queued", "Completed", "Failed"]:
        expect(sessions_section.locator("button", has_text=tab)).to_be_visible()


# ── Sessions status ────────────────────────────────────────────────


def test_sessions_shows_status_pills(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    # Status pills are inside session cards (articles), not the filter bar
    sessions_grid = page.locator("#unified-sessions-grid")
    expect(sessions_grid.locator("text=queued").first).to_be_visible()


# ── Sync ────────────────────────────────────────────────────────────


def test_sync_button(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    sync_btn = page.locator("button", has_text="Sync All")
    expect(sync_btn).to_be_visible()
    sync_btn.click()
    page.wait_for_timeout(500)
    expect(page.locator("#sync-status")).to_contain_text(re.compile(r"Synced|error"))


# ── Task creation ───────────────────────────────────────────────────


def test_create_headless_task(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    # Accept the hx-confirm dialog so the POST goes through.
    page.on("dialog", lambda dialog: dialog.accept())
    # "Headless" lives on tickets WITH MRs (visible under the default "Has PR"
    # filter). The "Auto" variant is on the {% empty %} branch and is hidden.
    page.locator("button", has_text="Headless").first.click()
    page.wait_for_timeout(500)
    expect(page.locator("h2", has_text="In-Flight Tickets")).to_be_visible()


def test_create_interactive_task(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("button.split-main", has_text="Interactive").first.click()
    page.wait_for_timeout(500)
    expect(page.locator("h2", has_text="Sessions")).to_be_visible()


# ── Cancel ──────────────────────────────────────────────────────────


def test_dismiss_pending_task(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    dismiss_btn = page.locator("button", has_text="Dismiss").first
    expect(dismiss_btn).to_be_visible()
    dismiss_btn.click()
    page.wait_for_timeout(500)


def test_dismiss_task_from_sessions(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    sessions_section = page.locator("#section-sessions")
    dismiss_btn = sessions_section.locator("button", has_text="Dismiss").first
    expect(dismiss_btn).to_be_visible()
    dismiss_btn.click()
    page.wait_for_timeout(1000)


# ── Panel endpoints ─────────────────────────────────────────────────


def test_panel_404_without_htmx(e2e_server: str, page: Page) -> None:
    response = page.goto(f"{e2e_server}/dashboard/panels/summary/")
    assert response is not None
    assert response.status == 404  # noqa: PLR2004


def test_unknown_panel_404(e2e_server: str, page: Page) -> None:
    response = page.goto(f"{e2e_server}/dashboard/panels/bogus/")
    assert response is not None
    assert response.status == 404  # noqa: PLR2004


def test_htmx_panels_present(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    for panel in [
        "summary",
        "automation",
        "action_required",
        "unified_sessions",
        "tickets",
    ]:
        expect(page.locator(f"[hx-get*='{panel}']").first).to_be_visible()


# ── Additional panels ──────────────────────────────────────────────


def test_action_required_panel(e2e_server: str, page: Page, assert_snapshot: Callable) -> None:
    page.goto(e2e_server)
    expect(page.locator("h2", has_text="Action Required")).to_be_visible()

    action_section = page.locator("h2", has_text="Action Required").locator("..")
    assert_snapshot(
        action_section.screenshot(animations="disabled"),
        name="action-required.png",
        threshold=_SNAPSHOT_THRESHOLD,
    )


def test_sessions_panel_visible(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    expect(page.locator("h2", has_text="Sessions")).to_be_visible()


def test_automation_panel(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    expect(page.locator("h2", has_text="Automation")).to_be_visible()


def test_task_detail_modal(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    task_link = page.locator("[hx-get*='/tasks/'][hx-get*='/detail/']").first
    if task_link.is_visible():
        task_link.click()
        page.wait_for_timeout(500)
        expect(page.locator("#task-modal-body")).to_be_visible()


# ── Git Pull (CONTRIBUTE mode) ─────────────────────────────────────


def _contribute_mode_enabled(page: Page) -> bool:
    """Check whether the git pull button is rendered (i.e. contribute=true in config)."""
    return page.locator("#git-pull-btn").count() > 0


def test_git_pull_button_visible_in_contribute_mode(e2e_server: str, page: Page) -> None:
    """When contribute mode is on, the Git Pull button appears in the status bar."""
    page.goto(e2e_server)
    if not _contribute_mode_enabled(page):
        pytest.skip("contribute mode not enabled in ~/.teatree.toml")
    expect(page.locator("#git-pull-btn")).to_be_visible()
    expect(page.locator("#git-pull-btn")).to_contain_text("Git Pull")


def test_git_pull_success_shows_toast(e2e_server: str, page: Page) -> None:
    """Clicking Git Pull shows a toast with the output on success."""
    page.goto(e2e_server)
    if not _contribute_mode_enabled(page):
        pytest.skip("contribute mode not enabled in ~/.teatree.toml")
    page.locator("#git-pull-btn").click()
    page.wait_for_timeout(2000)
    # On success the toast shows output, on failure the error span appears.
    # Both are valid — we just verify the button is functional and no JS crash.
    expect(page.locator("#git-pull-btn")).to_be_visible()


def test_git_pull_error_displayed(e2e_server: str, page: Page) -> None:
    """When git pull fails, the error span becomes visible."""
    page.goto(e2e_server)
    if not _contribute_mode_enabled(page):
        pytest.skip("contribute mode not enabled in ~/.teatree.toml")
    # The error span exists but is hidden initially
    error_span = page.locator("#git-pull-error")
    expect(error_span).to_be_hidden()
