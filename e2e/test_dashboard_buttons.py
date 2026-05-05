"""E2E coverage for every interactive dashboard surface — click-through regressions.

Complements ``test_dashboard.py`` (structure), ``test_dashboard_filters.py``
(search / filters), and ``test_dashboard_fixes.py`` (vendored JS / logo /
terminal menu) by exercising the buttons and menus that weren't yet clicked.

Each test exercises one clickable surface end-to-end (open menu → click →
observe UI state change or intercept network call) so any regression in HTMX
wiring, JS handlers, or backend routes trips a test.
"""

import json
import re
from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from e2e._dashboard_helpers import wait_for_dashboard_idle, wait_for_sessions, wait_for_tickets

_SNAPSHOT_THRESHOLD = 0.1


# ── Sync interval dropdown ─────────────────────────────────────────


def test_sync_interval_menu_toggles(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    menu = page.locator("#sync-interval-menu")
    expect(menu).to_be_hidden()
    page.locator('[onclick*="sync-interval-menu"]').click()
    expect(menu).to_be_visible()
    expect(menu.locator("button.sync-interval-opt")).to_have_count(7)


def test_sync_interval_sets_localstorage_and_label(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    page.locator('[onclick*="sync-interval-menu"]').click()
    page.locator("button.sync-interval-opt[data-interval='5']").click()
    stored = page.evaluate("localStorage.getItem('teatree_sync_interval')")
    assert stored == "5"
    expect(page.locator("#sync-label")).to_contain_text("Auto: 5m")


def test_sync_interval_manual_resets_label(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    page.locator('[onclick*="sync-interval-menu"]').click()
    page.locator("button.sync-interval-opt[data-interval='5']").click()
    expect(page.locator("#sync-label")).to_contain_text("Auto: 5m")
    page.locator('[onclick*="sync-interval-menu"]').click()
    page.locator("button.sync-interval-opt[data-interval='0']").click()
    expect(page.locator("#sync-label")).to_contain_text("Sync All")


# ── Branch switcher ────────────────────────────────────────────────


def test_branch_switcher_toggles_popup(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    switcher = page.locator("#branch-switcher button").first
    expect(switcher).to_be_visible()
    popup = page.locator("#branch-selector-popup")
    expect(popup).to_be_hidden()
    switcher.click()
    expect(popup).to_be_visible()


def test_branch_switcher_lists_branches(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    # Stub the branches endpoint — the test container has no checkout, so the
    # real GET would return empty. Route fulfill gives deterministic data.
    page.route(
        "**/dashboard/switch-branch/",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"branches": ["main", "feat/x"], "current": "main"}),
        ),
    )
    page.locator("#branch-switcher button").first.click()
    branch_list = page.locator("#branch-list")
    expect(branch_list).to_contain_text("main")
    expect(branch_list).to_contain_text("feat/x")


# ── Task modal close paths ─────────────────────────────────────────


def test_task_modal_closes_on_backdrop_click(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    page.locator("button[onclick^='openTaskModal']").first.click()
    modal = page.locator("#task-modal")
    expect(modal).to_be_visible()
    page.locator("#task-modal > div.absolute.inset-0").click(position={"x": 5, "y": 5})
    expect(modal).to_be_hidden()


def test_task_modal_closes_on_escape(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    page.locator("button[onclick^='openTaskModal']").first.click()
    modal = page.locator("#task-modal")
    expect(modal).to_be_visible()
    page.keyboard.press("Escape")
    expect(modal).to_be_hidden()


# ── Ticket detail toggle (approval button expands task graph + lifecycle) ──


def test_approval_button_expands_ticket_details(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    wait_for_tickets(page)
    approval_btn = page.locator("button[onclick^='toggleTicketDetails']").first
    # The expanded row id follows pattern #ticket-details-<id> and starts hidden
    expanded_row = page.locator("tr[id^='ticket-details-']").first
    expect(expanded_row).to_be_hidden()
    approval_btn.click()
    expect(expanded_row).to_be_visible()
    expect(expanded_row).to_contain_text("Task Graph")
    expect(expanded_row).to_contain_text("Lifecycle")


# ── Hide selected tickets (bulk transition POST) ───────────────────


def test_hide_selected_fires_transition(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    wait_for_tickets(page)
    page.locator("thead input[type='checkbox']").check()
    hide_btn = page.locator("#hide-selected-btn")
    expect(hide_btn).to_be_visible()
    with page.expect_request(lambda r: "/transition/" in r.url and r.method == "POST") as req_info:
        hide_btn.click()
    assert req_info.value.method == "POST"


def test_hide_button_hidden_when_no_selection(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    wait_for_tickets(page)
    expect(page.locator("#hide-selected-btn")).to_be_hidden()


# ── Sessions filter tabs click-through ─────────────────────────────


def test_sessions_filter_queued_hides_others(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    wait_for_sessions(page)
    grid = page.locator("#unified-sessions-grid")
    # Seeded: one interactive (queued) + one headless that the immediate backend
    # runs synchronously (ends up in "completed" status). Clicking "Queued" must
    # hide the completed article.
    all_articles_before = grid.locator("article").all()
    assert len(all_articles_before) >= 1
    page.locator('button[onclick*="filterSessions"][onclick*="\'queued\'"]').click()
    # At least one article marked queued stays visible; any non-queued is hidden.
    for article in grid.locator("article").all():
        status = article.get_attribute("data-session-status")
        if status == "queued":
            expect(article).to_be_visible()
        else:
            expect(article).to_be_hidden()


def test_sessions_filter_all_shows_everything(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    wait_for_sessions(page)
    grid = page.locator("#unified-sessions-grid")
    page.locator('button[onclick*="filterSessions"][onclick*="\'queued\'"]').click()
    page.locator('button[onclick*="filterSessions"][onclick*="\'all\'"]').click()
    for article in grid.locator("article").all():
        expect(article).to_be_visible()


# ── Top-bar Terminal + Agent launch buttons (verify HTTP POST fires) ──


def test_terminal_button_posts_launch_terminal(e2e_server: str, page: Page) -> None:
    # Stub the endpoint — the real view spawns an external terminal process on
    # the developer's machine, which we do NOT want firing from a test run.
    page.route(
        "**/dashboard/launch-terminal/",
        lambda route: route.fulfill(status=200, content_type="application/json", body="{}"),
    )
    page.goto(e2e_server)
    with page.expect_request("**/dashboard/launch-terminal/") as req_info:
        page.locator("button.split-main", has_text="Terminal").first.click()
    assert req_info.value.method == "POST"


def test_agent_button_posts_launch_interactive_agent(e2e_server: str, page: Page) -> None:
    # Stub — the real view spawns a Claude agent in an external terminal.
    page.route(
        "**/dashboard/launch-agent/",
        lambda route: route.fulfill(status=200, content_type="application/json", body="{}"),
    )
    page.goto(e2e_server)
    with page.expect_request("**/dashboard/launch-agent/") as req_info:
        page.locator("button.split-main", has_text="Agent").first.click()
    assert req_info.value.method == "POST"


# ── SSE connection wiring ──────────────────────────────────────────


def test_sse_extension_wrapper_mounted(e2e_server: str, page: Page) -> None:
    """The htmx SSE extension wrapper points at the dashboard-events URL."""
    page.goto(e2e_server)
    wrapper = page.locator("[hx-ext='sse']").first
    connect = wrapper.get_attribute("sse-connect") or ""
    assert connect.endswith("/dashboard/events/"), connect


# ── Automation panel content (not just heading) ───────────────────


def test_automation_panel_renders_body(e2e_server: str, page: Page) -> None:
    """Automation body (htmx-loaded) resolves past the loading include.

    Asserts against content only present in ``dashboard_automation.html`` — the
    cards or the empty-state fallback — so the heading-only include cannot
    satisfy the test.
    """
    page.goto(e2e_server)
    panel = page.locator("h2", has_text="Automation").locator("xpath=ancestor::details[1]")
    body = panel.locator("div[hx-get]")
    expect(body).to_contain_text(re.compile(r"Succeeded \(24h\)|No headless activity"))


# ── Pending Reviews section visible ────────────────────────────────


def test_pending_reviews_heading_visible(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    expect(page.locator("h2", has_text="Pending Reviews")).to_be_visible()


# ── Overlay selector persists to localStorage ──────────────────────


def test_overlay_selector_persists_choice(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    selector = page.locator("#overlay-selector")
    if selector.count() == 0:
        pytest.skip("no overlays registered in test env")
    options = selector.locator("option").all()
    non_empty = [o for o in options if (o.get_attribute("value") or "")]
    if not non_empty:
        pytest.skip("no non-empty overlay options")
    value = non_empty[0].get_attribute("value")
    assert value is not None
    selector.select_option(value)
    stored = page.evaluate("localStorage.getItem('teatree_overlay')")
    assert stored == value


# ── Section collapse / expand (<details>) ──────────────────────────


def test_sessions_section_can_collapse(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    section = page.locator("#section-sessions")
    expect(section).to_have_attribute("open", "")
    section.locator("summary").first.click()
    # After click, open attribute is removed
    expect(section).not_to_have_attribute("open", "")


# ── In-Flight Tickets Sync button (separate from top-bar Sync) ────


def test_inflight_tickets_header_sync_posts(e2e_server: str, page: Page) -> None:
    """Sync button in the In-Flight Tickets summary fires the followup endpoint."""
    page.goto(e2e_server)
    in_flight = page.locator("h2", has_text="In-Flight Tickets").locator("..")
    sync_btn = in_flight.locator("button", has_text="Sync").first
    expect(sync_btn).to_be_visible()
    with page.expect_request("**/dashboard/sync/") as req_info:
        sync_btn.click()
    assert req_info.value.method == "POST"


# ── Ticket transition POST (accept confirm dialog) ────────────────


def test_ticket_transition_button_posts(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    wait_for_tickets(page)
    # Ticket #42 is in state "started" → offers transitions; grab first one.
    transition_btn = page.locator("button[hx-post*='/transition/']").first
    if transition_btn.count() == 0:
        pytest.skip("seed ticket has no available transitions")
    page.on("dialog", lambda dialog: dialog.accept())
    with page.expect_response("**/transition/") as resp_info:
        transition_btn.click()
    assert resp_info.value.request.method == "POST"


# ── Sessions grid cancel button (force cancel / dismiss) ──────────


def test_sessions_dismiss_posts_cancel(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    wait_for_sessions(page)
    sessions = page.locator("#section-sessions")
    dismiss_btn = sessions.locator("button", has_text="Dismiss").first
    if dismiss_btn.count() == 0:
        pytest.skip("no queued tasks in seed")
    with page.expect_response("**/cancel/") as resp_info:
        dismiss_btn.click()
    assert resp_info.value.request.method == "POST"


# ── Golden screenshots for interaction states ─────────────────────
#
# pytest-playwright-visual auto-generates baselines on first run and writes
# them to ``e2e/snapshots/<test_name>/``. The canonical environment is the
# ``linux/amd64`` e2e Docker image — regenerate with
# ``t3 teatree e2e project --update-snapshots``. Baselines regenerated on a
# macOS laptop will not match CI.


def _dismiss_toasts(page: Page) -> None:
    page.evaluate("document.getElementById('toast-stack').innerHTML = ''")


def test_snapshot_sync_interval_menu_open(e2e_server: str, page: Page, assert_snapshot: Callable) -> None:
    page.goto(e2e_server)
    page.wait_for_timeout(1000)
    _dismiss_toasts(page)
    page.locator('[onclick*="sync-interval-menu"]').click()
    menu = page.locator("#sync-interval-menu")
    expect(menu).to_be_visible()
    assert_snapshot(
        menu.screenshot(animations="disabled"),
        name="sync-interval-menu.png",
        threshold=_SNAPSHOT_THRESHOLD,
    )


def test_snapshot_branch_switcher_popup(e2e_server: str, page: Page, assert_snapshot: Callable) -> None:
    page.route(
        "**/dashboard/switch-branch/",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"branches": ["main", "feat/x"], "current": "main"}),
        ),
    )
    page.goto(e2e_server)
    page.wait_for_timeout(1000)
    _dismiss_toasts(page)
    page.locator("#branch-switcher button").first.click()
    popup = page.locator("#branch-selector-popup")
    expect(popup).to_be_visible()
    page.wait_for_timeout(300)  # let branch list render
    assert_snapshot(
        popup.screenshot(animations="disabled"),
        name="branch-switcher-popup.png",
        threshold=_SNAPSHOT_THRESHOLD,
    )


def test_snapshot_task_modal_open(e2e_server: str, page: Page, assert_snapshot: Callable) -> None:
    page.goto(e2e_server)
    page.wait_for_timeout(1000)
    _dismiss_toasts(page)
    page.locator("button[onclick^='openTaskModal']").first.click()
    modal = page.locator("#task-modal-body")
    expect(modal).to_be_visible()
    page.wait_for_timeout(500)  # let htmx body fetch complete
    assert_snapshot(
        modal.screenshot(animations="disabled"),
        name="task-modal-body.png",
        threshold=_SNAPSHOT_THRESHOLD,
    )


def test_snapshot_ticket_details_expanded(e2e_server: str, page: Page, assert_snapshot: Callable) -> None:
    page.goto(e2e_server)
    page.wait_for_timeout(1000)
    _dismiss_toasts(page)
    wait_for_tickets(page)
    page.locator("button[onclick^='toggleTicketDetails']").first.click()
    details = page.locator("tr[id^='ticket-details-']").first
    expect(details).to_be_visible()
    # Wait for the mermaid lifecycle SVG to render. Mermaid is vendored locally
    # (``teatree/js/mermaid-11.min.js``) so this works in sealed CI / Docker.
    wait_for_dashboard_idle(page)
    details.locator(".mermaid").first.wait_for(state="attached")
    details.locator(".mermaid svg").first.wait_for(state="attached", timeout=15000)
    assert_snapshot(
        details.screenshot(animations="disabled"),
        name="ticket-details-expanded.png",
        threshold=_SNAPSHOT_THRESHOLD,
    )


def test_mermaid_lifecycle_renders_svg(e2e_server: str, page: Page) -> None:
    """Lifecycle ``<pre class='mermaid'>`` is replaced with an inline SVG."""
    page.goto(e2e_server)
    wait_for_tickets(page)
    page.locator("button[onclick^='toggleTicketDetails']").first.click()
    details = page.locator("tr[id^='ticket-details-']").first
    expect(details).to_be_visible()
    wait_for_dashboard_idle(page)
    details.locator(".mermaid").first.wait_for(state="attached")
    details.locator(".mermaid svg").first.wait_for(state="attached", timeout=15000)


def test_snapshot_sessions_filter_queued(e2e_server: str, page: Page, assert_snapshot: Callable) -> None:
    page.goto(e2e_server)
    wait_for_sessions(page)
    page.wait_for_timeout(1000)
    _dismiss_toasts(page)
    grid = page.locator("#unified-sessions-grid")
    page.locator('button[onclick*="filterSessions"][onclick*="\'queued\'"]').click()
    assert_snapshot(
        grid.screenshot(animations="disabled"),
        name="sessions-filter-queued.png",
        threshold=_SNAPSHOT_THRESHOLD,
    )
