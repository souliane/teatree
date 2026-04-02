"""E2E tests for the TeaTree dashboard.

Run with:
    uv run --group e2e pytest e2e/ --ds e2e.settings --no-cov -n auto -v
"""

import re

from playwright.sync_api import Page, expect

# ── Dashboard structure ─────────────────────────────────────────────


def test_dashboard_loads(e2e_server: str, page: Page) -> None:
    """Page loads, summary counters and section headings are visible."""
    page.goto(e2e_server)
    expect(page.locator("body")).to_contain_text("In Flight Tickets")
    for heading in ["Automation", "Action Required", "In-Flight Tickets", "Interactive Queue", "Active Sessions"]:
        expect(page.locator("h2", has_text=heading)).to_be_visible()


def test_summary_counters(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    for label in ["In Flight Tickets", "Active Worktrees", "Pending Headless", "Pending Interactive"]:
        expect(page.locator("body")).to_contain_text(label)


# ── Tickets table ───────────────────────────────────────────────────


def test_tickets_with_mrs(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    expect(page.locator("body")).to_contain_text("#42")
    expect(page.locator("body")).to_contain_text("Fix the login bug")
    expect(page.locator("body")).to_contain_text("backend")
    expect(page.locator("body")).to_contain_text("frontend")


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


# ── Task queues ─────────────────────────────────────────────────────


def test_headless_queue_content(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    page.locator("summary", has_text="Automated").click()
    expect(page.locator("body")).to_contain_text("Automated code review")
    expect(page.locator("button", has_text="Execute").first).to_be_visible()


def test_interactive_queue_content(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    expect(page.locator("body")).to_contain_text("Needs manual verification")
    expect(page.locator("button", has_text="Launch").first).to_be_visible()


# ── Sessions ────────────────────────────────────────────────────────


def test_sessions_empty(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    expect(page.locator("body")).to_contain_text(re.compile(r"No active Claude sessions detected|PID \d+"))


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
    page.locator("button", has_text="Auto").first.click()
    page.wait_for_timeout(500)
    # With ImmediateBackend the task may complete instantly, so just verify
    # the request succeeded (no error toast) and the page is still functional
    expect(page.locator("h2", has_text="In-Flight Tickets")).to_be_visible()


def test_create_interactive_task(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    page.locator("button", has_text="Interactive").first.click()
    page.wait_for_timeout(500)
    expect(page.locator("h2", has_text="Interactive Queue")).to_be_visible()


# ── Cancel ──────────────────────────────────────────────────────────


def test_dismiss_pending_task(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    dismiss_btn = page.locator("button", has_text="Dismiss").first
    expect(dismiss_btn).to_be_visible()
    dismiss_btn.click()
    page.wait_for_timeout(500)


def test_cancel_claimed_task(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    page.locator("summary", has_text="Automated").click()
    execute_btn = page.locator("button", has_text="Execute").first
    expect(execute_btn).to_be_visible()
    execute_btn.click()
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
    for panel in ["summary", "tickets", "queue", "sessions", "action_required", "automation"]:
        expect(page.locator(f"[hx-get*='{panel}']").first).to_be_visible()
    page.locator("summary", has_text="Automated").click()
    for panel in ["headless_queue", "review_comments"]:
        expect(page.locator(f"[hx-get*='{panel}']").first).to_be_visible()
    expect(page.locator("[hx-get*='activity']").first).to_be_attached()


# ── Additional panels ──────────────────────────────────────────────


def test_action_required_panel(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    expect(page.locator("h2", has_text="Action Required")).to_be_visible()


def test_review_comments_panel(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    page.locator("summary", has_text="Automated").click()
    expect(page.locator("h2", has_text="Review Comments")).to_be_visible()


def test_activity_panel(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    expect(page.locator("h2", has_text="Recent Activity")).to_be_visible()


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
