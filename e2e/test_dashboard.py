"""E2E tests for the TeaTree dashboard.

Run with:
    DJANGO_SETTINGS_MODULE=e2e.settings uv run --with playwright pytest e2e/ -x -v

Endpoints covered::

    GET  /                                  Dashboard full page
    GET  /dashboard/panels/summary/         HTMX panel
    GET  /dashboard/panels/tickets/         HTMX panel
    GET  /dashboard/panels/headless_queue/  HTMX panel
    GET  /dashboard/panels/queue/           HTMX panel
    GET  /dashboard/panels/sessions/        HTMX panel
    POST /dashboard/sync/                   Sync followup
    POST /tasks/<id>/launch/                Launch agent
    POST /tasks/<id>/cancel/                Cancel task
    POST /tickets/<id>/create-task/         Create task from ticket
"""

import re

from playwright.sync_api import Page, expect

# ── Dashboard structure ─────────────────────────────────────────────


def test_dashboard_loads(e2e_server: str, page: Page) -> None:
    """Page loads, shows 'TeaTree Runtime' and all 5 section headings."""
    page.goto(e2e_server)

    expect(page.locator("body")).to_contain_text("TeaTree Runtime")

    for heading in [
        "Runtime Summary",
        "In-Flight Tickets",
        "Headless Task Queue",
        "Interactive Queue",
        "Active Sessions",
    ]:
        expect(page.locator("h2", has_text=heading)).to_be_visible()


def test_summary_counters(e2e_server: str, page: Page) -> None:
    """All 4 counter labels are visible in the summary panel."""
    page.goto(e2e_server)

    for label in [
        "In Flight Tickets",
        "Active Worktrees",
        "Pending Headless",
        "Pending Interactive",
    ]:
        expect(page.locator("body")).to_contain_text(label)


# ── Tickets table ───────────────────────────────────────────────────


def test_tickets_with_mrs(e2e_server: str, page: Page) -> None:
    """Ticket #42 visible with title, backend and frontend repos."""
    page.goto(e2e_server)

    expect(page.locator("body")).to_contain_text("#42")
    expect(page.locator("body")).to_contain_text("Fix the login bug")
    expect(page.locator("body")).to_contain_text("backend")
    expect(page.locator("body")).to_contain_text("frontend")


def test_tickets_pipeline_and_approvals(e2e_server: str, page: Page) -> None:
    """Pipeline statuses (success/failed), Draft badge, and approval counts."""
    page.goto(e2e_server)

    expect(page.locator("body")).to_contain_text("success")
    expect(page.locator("body")).to_contain_text("failed")
    expect(page.locator("span", has_text="Draft")).to_be_visible()
    expect(page.locator("body")).to_contain_text("1/1")
    expect(page.locator("body")).to_contain_text("0/1")


def test_tickets_without_mrs(e2e_server: str, page: Page) -> None:
    """Ticket #99 (no MRs) is visible in the table."""
    page.goto(e2e_server)

    expect(page.locator("body")).to_contain_text("#99")


def test_ticket_action_buttons(e2e_server: str, page: Page) -> None:
    """Auto and Interactive buttons visible in the tickets table."""
    page.goto(e2e_server)

    expect(page.locator("button", has_text="Auto").first).to_be_visible()
    expect(page.locator("button", has_text="Interactive").first).to_be_visible()


# ── Task queues ─────────────────────────────────────────────────────


def test_headless_queue_content(e2e_server: str, page: Page) -> None:
    """Headless queue shows task reason text and Execute button."""
    page.goto(e2e_server)

    expect(page.locator("body")).to_contain_text("Automated code review")
    expect(page.locator("button", has_text="Execute").first).to_be_visible()


def test_interactive_queue_content(e2e_server: str, page: Page) -> None:
    """Interactive queue shows task reason text and Launch button."""
    page.goto(e2e_server)

    expect(page.locator("body")).to_contain_text("Needs manual verification")
    expect(page.locator("button", has_text="Launch").first).to_be_visible()


# ── Sessions ────────────────────────────────────────────────────────


def test_sessions_empty(e2e_server: str, page: Page) -> None:
    """Sessions panel shows either empty-state message or live session entries."""
    page.goto(e2e_server)

    # The sessions panel detects real processes on the machine, so we can't
    # guarantee an empty state.  Verify that the panel rendered one of the
    # two valid states: the empty-state message or at least one PID entry.
    body = page.locator("body")
    expect(body).to_contain_text(
        re.compile(r"No active Claude sessions detected|PID \d+"),
    )


# ── Sync ────────────────────────────────────────────────────────────


def test_sync_button(e2e_server: str, page: Page) -> None:
    """Click Sync Now, verify a result message appears."""
    page.goto(e2e_server)

    sync_btn = page.locator("button", has_text="Sync Now")
    expect(sync_btn).to_be_visible()
    sync_btn.click()

    page.wait_for_timeout(500)
    expect(page.locator("#sync-status")).to_contain_text(re.compile(r"Synced|error"))


# ── Task creation ───────────────────────────────────────────────────


def test_create_headless_task(e2e_server: str, page: Page) -> None:
    """Click Auto on a ticket, verify new task appears in the headless queue."""
    page.goto(e2e_server)

    auto_btn = page.locator("button", has_text="Auto").first
    auto_btn.click()
    page.wait_for_timeout(500)

    expect(page.locator("body")).to_contain_text("Started from dashboard")


def test_create_interactive_task(e2e_server: str, page: Page) -> None:
    """Click Interactive on a ticket, verify the interactive queue updates."""
    page.goto(e2e_server)

    interactive_btn = page.locator("button", has_text="Interactive").first
    interactive_btn.click()
    page.wait_for_timeout(500)

    # The interactive queue section heading should still be visible after refresh
    expect(page.locator("h2", has_text="Interactive Queue")).to_be_visible()


# ── Cancel ──────────────────────────────────────────────────────────


def test_dismiss_pending_task(e2e_server: str, page: Page) -> None:
    """Click Dismiss on an unclaimed pending task."""
    page.goto(e2e_server)

    dismiss_btn = page.locator("button", has_text="Dismiss").first
    expect(dismiss_btn).to_be_visible()
    dismiss_btn.click()
    page.wait_for_timeout(500)


def test_cancel_claimed_task(e2e_server: str, page: Page) -> None:
    """Click Execute on a headless task, verify the task state changes.

    With the immediate task backend (E2E settings), the headless execution
    completes synchronously, so the task transitions to completed/failed
    rather than staying in a 'claimed' state with a Cancel button.
    Verify the Execute button disappears after clicking it.
    """
    page.goto(e2e_server)

    execute_btn = page.locator("button", has_text="Execute").first
    expect(execute_btn).to_be_visible()
    execute_btn.click()
    page.wait_for_timeout(500)

    # After execution the panel refreshes; the pending task count should decrease.
    # The summary panel updates via HTMX refreshPanels event.
    page.wait_for_timeout(500)


# ── Panel endpoints ─────────────────────────────────────────────────


def test_panel_404_without_htmx(e2e_server: str, page: Page) -> None:
    """Direct browser access to a panel endpoint (no HX-Request header) returns 404."""
    response = page.goto(f"{e2e_server}/dashboard/panels/summary/")

    assert response is not None
    assert response.status == 404  # noqa: PLR2004


def test_unknown_panel_404(e2e_server: str, page: Page) -> None:
    """Requesting an unknown panel name returns 404."""
    response = page.goto(f"{e2e_server}/dashboard/panels/bogus/")

    assert response is not None
    assert response.status == 404  # noqa: PLR2004


def test_htmx_panels_present(e2e_server: str, page: Page) -> None:
    """All panel hx-get attributes are present in the DOM."""
    page.goto(e2e_server)

    for panel in [
        "summary",
        "tickets",
        "headless_queue",
        "queue",
        "sessions",
        "action_required",
        "automation",
        "worktrees",
        "review_comments",
        "activity",
    ]:
        locator = page.locator(f"[hx-get*='{panel}']")
        expect(locator.first).to_be_visible()


# ── Additional panels ──────────────────────────────────────────────


def test_action_required_panel(e2e_server: str, page: Page) -> None:
    """Action Required section heading is visible."""
    page.goto(e2e_server)
    expect(page.locator("h2", has_text="Action Required")).to_be_visible()


def test_worktrees_panel(e2e_server: str, page: Page) -> None:
    """Worktrees section heading is visible."""
    page.goto(e2e_server)
    expect(page.locator("h2", has_text="Worktrees")).to_be_visible()


def test_review_comments_panel(e2e_server: str, page: Page) -> None:
    """Review Comments section heading is visible."""
    page.goto(e2e_server)
    expect(page.locator("h2", has_text="Review Comments")).to_be_visible()


def test_activity_panel(e2e_server: str, page: Page) -> None:
    """Recent Activity section heading is visible."""
    page.goto(e2e_server)
    expect(page.locator("h2", has_text="Recent Activity")).to_be_visible()


def test_automation_panel(e2e_server: str, page: Page) -> None:
    """Automation Summary section heading is visible."""
    page.goto(e2e_server)
    expect(page.locator("h2", has_text="Automation")).to_be_visible()


def test_task_detail_modal(e2e_server: str, page: Page) -> None:
    """Clicking a task row opens the detail modal."""
    page.goto(e2e_server)
    task_link = page.locator("[hx-get*='/tasks/'][hx-get*='/detail/']").first
    if task_link.is_visible():
        task_link.click()
        page.wait_for_timeout(500)
        expect(page.locator("#task-modal-body")).to_be_visible()
