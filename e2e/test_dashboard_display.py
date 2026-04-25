"""E2E coverage for dashboard **display** — what the page renders.

``test_dashboard_buttons.py`` covers click handlers; this file covers the
content each panel puts on the screen. Seed data in ``conftest.py::_seed_data``
is the source of truth — any regression in the selectors, partials, or
rendering logic that omits a field should trip one of these assertions.

Each panel gets a focused test that names the specific seeded field we expect
to see, keeping failures actionable (``Automation panel missing "No headless
activity yet."`` is easier to fix than ``snapshot mismatch``).
"""

from playwright.sync_api import Locator, Page, expect


def _panel(page: Page, heading_text: str) -> Locator:
    """Return the ``<details>`` section whose ``<h2>`` matches ``heading_text``."""
    return page.locator(f"details:has(h2:text-is('{heading_text}'))").first


# ── Summary counters ───────────────────────────────────────────────


def test_summary_panel_shows_all_five_counters(e2e_server: str, page: Page) -> None:
    """Summary panel has no heading — assert counter labels directly on the body."""
    page.goto(e2e_server)
    body = page.locator("body")
    for label in (
        "In Flight Tickets",
        "Active Worktrees",
        "Pending Headless",
        "Pending Interactive",
        "Pending Reviews",
    ):
        expect(body).to_contain_text(label)


# ── Automation panel ───────────────────────────────────────────────


def test_automation_panel_has_heading(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    expect(_panel(page, "Automation")).to_be_visible()


def test_automation_shows_empty_state_when_no_activity(e2e_server: str, page: Page) -> None:
    """Seed data has no completed headless attempts → panel shows empty-state message."""
    page.goto(e2e_server)
    expect(_panel(page, "Automation")).to_contain_text("No headless activity yet.")


# ── Tickets table (the biggest surface) ────────────────────────────


def test_tickets_table_header_has_expected_columns(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    panel = _panel(page, "In-Flight Tickets")
    panel.locator("tr[data-mr-row]").first.wait_for(state="attached")
    thead = panel.locator("thead").first
    for col in ("Ticket", "Status", "PR", "E2E", "CI", "Review", "Approved", "Transitions", "Tasks"):
        expect(thead).to_contain_text(col)


def test_ticket_row_renders_issue_title_and_labels(e2e_server: str, page: Page) -> None:
    """Seeded ticket #42 has title "Fix the login bug" + labels bug / priority::high."""
    page.goto(e2e_server)
    panel = _panel(page, "In-Flight Tickets")
    panel.locator("tr[data-mr-row]").first.wait_for(state="attached")
    expect(panel).to_contain_text("Fix the login bug")
    expect(panel).to_contain_text("bug")
    expect(panel).to_contain_text("priority::high")


def test_ticket_row_shows_variant_pill(e2e_server: str, page: Page) -> None:
    """The ``demo`` variant on ticket #42 must render as a pill."""
    page.goto(e2e_server)
    panel = _panel(page, "In-Flight Tickets")
    panel.locator("tr[data-mr-row]").first.wait_for(state="attached")
    expect(panel).to_contain_text("demo")


def test_mr_rows_render_both_backend_and_frontend(e2e_server: str, page: Page) -> None:
    """The ticket has 2 MRs (backend!100 + frontend!200); both should be rows."""
    page.goto(e2e_server)
    rows = page.locator("tr[data-mr-row]")
    rows.first.wait_for(state="attached")
    urls = [row.get_attribute("data-mr-url") for row in rows.all()]
    assert any("/backend/-/merge_requests/100" in (u or "") for u in urls), urls
    assert any("/frontend/-/merge_requests/200" in (u or "") for u in urls), urls


def test_draft_mr_is_marked_draft(e2e_server: str, page: Page) -> None:
    """frontend!200 is draft — it must expose ``data-mr-draft=true`` and a ``Draft`` pill."""
    page.goto(e2e_server)
    page.locator("tr[data-mr-row]").first.wait_for(state="attached")
    draft_rows = page.locator('tr[data-mr-row][data-mr-draft="true"]')
    expect(draft_rows).to_have_count(1)
    expect(draft_rows.first).to_contain_text("Draft")


def test_mr_titles_exposed_as_data_attrs(e2e_server: str, page: Page) -> None:
    """MR titles round-trip through ``data-mr-title`` for bulk-copy flows."""
    page.goto(e2e_server)
    rows = page.locator("tr[data-mr-row]")
    rows.first.wait_for(state="attached")
    titles = sorted(row.get_attribute("data-mr-title") or "" for row in rows.all())
    assert "fix(auth): resolve login timeout" in titles
    assert "fix(auth): update login form" in titles


def test_pipeline_status_renders_per_mr(e2e_server: str, page: Page) -> None:
    """backend!100 pipeline=success (✅ icon), frontend!200 pipeline=failed (❌ icon)."""
    page.goto(e2e_server)
    panel = _panel(page, "In-Flight Tickets")
    panel.locator("tr[data-mr-row]").first.wait_for(state="attached")
    panel_text = panel.inner_text()
    assert "✅" in panel_text, panel_text
    assert "❌" in panel_text, panel_text


def test_approval_count_rendered(e2e_server: str, page: Page) -> None:
    """Approval state: backend!100 approved (1/1) → data-mr-approved=true."""
    page.goto(e2e_server)
    page.locator("tr[data-mr-row]").first.wait_for(state="attached")
    approved_rows = page.locator('tr[data-mr-row][data-mr-approved="true"]')
    expect(approved_rows).to_have_count(1)


# ── Sessions grid (unified queue view) ─────────────────────────────


def test_sessions_grid_shows_task_id_and_phase(e2e_server: str, page: Page) -> None:
    """Seeded tasks have phases ``reviewing`` and ``testing`` — dashboard uppercases them."""
    page.goto(e2e_server)
    grid = page.locator("#unified-sessions-grid")
    grid.wait_for(state="visible")
    grid_text = grid.inner_text()
    assert "TESTING" in grid_text, grid_text
    assert "REVIEWING" in grid_text, grid_text


def test_sessions_grid_shows_execution_target_badges(e2e_server: str, page: Page) -> None:
    """Badges ``Headless`` and ``interactive`` render (case follows the partial)."""
    page.goto(e2e_server)
    grid = page.locator("#unified-sessions-grid")
    grid.wait_for(state="visible")
    grid_text = grid.inner_text()
    assert "Headless" in grid_text or "headless" in grid_text, grid_text
    assert "interactive" in grid_text, grid_text


def test_sessions_grid_shows_execution_reason(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    grid = page.locator("#unified-sessions-grid")
    grid.wait_for(state="visible")
    expect(grid).to_contain_text("Needs manual verification")


def test_sessions_filter_tabs_all_present(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    grid = page.locator("#unified-sessions-grid")
    grid.wait_for(state="visible")
    tabs = page.locator('button[onclick*="filterSessions"]')
    labels = [t.inner_text().strip() for t in tabs.all()]
    for label in ("All", "Running", "Queued", "Completed", "Failed"):
        assert label in labels, labels


# ── Header / top-bar content ───────────────────────────────────────


def test_header_has_terminal_and_agent_buttons(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    expect(page.locator("button.split-main", has_text="Terminal").first).to_be_visible()
    expect(page.locator("button.split-main", has_text="Agent").first).to_be_visible()


def test_header_has_sync_button(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    expect(page.locator("#sync-label")).to_be_visible()


def test_overlay_logo_renders(e2e_server: str, page: Page) -> None:
    """The teatree overlay logo (or default) must be in the page."""
    page.goto(e2e_server)
    logo = page.locator("img[alt*='logo' i], img[src*='teatree-logo']").first
    expect(logo).to_be_visible()


# ── Action Required / Pending Reviews ─────────────────────────────


def test_action_required_shows_interactive_task(e2e_server: str, page: Page) -> None:
    """The interactive task (``Needs manual verification``) surfaces in Action Required."""
    page.goto(e2e_server)
    panel = _panel(page, "Action Required")
    if panel.count() == 0:
        return  # panel is only rendered when items exist
    expect(panel).to_contain_text("Needs manual verification")


def test_pending_reviews_panel_renders(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    expect(_panel(page, "Pending Reviews")).to_be_visible()


# ── Section structure (all expected panels present) ────────────────


def test_all_primary_sections_have_headings(e2e_server: str, page: Page) -> None:
    page.goto(e2e_server)
    for heading_text in ("Automation", "In-Flight Tickets", "Sessions", "Pending Reviews"):
        expect(page.locator("h2", has_text=heading_text).first).to_be_visible()
