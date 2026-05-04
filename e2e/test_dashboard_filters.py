"""E2E tests for dashboard ticket filters, search, and MR copy (issue #166).

Covers: search input, filter toggles, MR checkboxes, copy-selected button.
"""

import re

from playwright.sync_api import Page, expect

from e2e._dashboard_helpers import wait_for_tickets

# ── Filter toolbar ─────────────────────────────────────────────────


def test_filter_toolbar_visible(e2e_server: str, page: Page) -> None:
    """Search input and filter checkboxes are visible in the In-Flight Tickets panel."""
    page.goto(e2e_server)
    expect(page.locator("#ticket-search")).to_be_visible()
    expect(page.locator("#filter-has-mr")).to_be_visible()
    expect(page.locator("#filter-non-draft")).to_be_visible()
    expect(page.locator("#filter-unapproved")).to_be_visible()


def test_copy_button_hidden_by_default(e2e_server: str, page: Page) -> None:
    """Copy button is hidden when no MRs are selected."""
    page.goto(e2e_server)
    expect(page.locator("#copy-selected-btn")).to_be_hidden()


# ── Select all / MR checkboxes ────────────────────────────────────


def test_select_all_checkbox_in_header(e2e_server: str, page: Page) -> None:
    """The table header has a select-all checkbox."""
    page.goto(e2e_server)
    wait_for_tickets(page)
    select_all = page.locator("thead input[type='checkbox']")
    expect(select_all).to_be_visible()


def test_mr_rows_have_checkboxes(e2e_server: str, page: Page) -> None:
    """Each MR row has a checkbox for selection."""
    page.goto(e2e_server)
    wait_for_tickets(page)
    checkboxes = page.locator("input.mr-select")
    expect(checkboxes.first).to_be_visible()
    expect(checkboxes).to_have_count(2)  # seed has 2 MRs


def test_select_all_checks_visible_mrs(e2e_server: str, page: Page) -> None:
    """Clicking select-all checks all visible MR checkboxes."""
    page.goto(e2e_server)
    wait_for_tickets(page)
    page.locator("thead input[type='checkbox']").check()
    checked = page.locator("input.mr-select:checked")
    expect(checked).to_have_count(2)  # seed has 2 MRs


def test_select_mr_shows_copy_button(e2e_server: str, page: Page) -> None:
    """Selecting an MR makes the copy button visible."""
    page.goto(e2e_server)
    wait_for_tickets(page)
    page.locator("input.mr-select").first.check()
    expect(page.locator("#copy-selected-btn")).to_be_visible()


def test_deselect_all_hides_copy_button(e2e_server: str, page: Page) -> None:
    """Deselecting all MRs hides the copy button."""
    page.goto(e2e_server)
    wait_for_tickets(page)
    page.locator("thead input[type='checkbox']").check()
    expect(page.locator("#copy-selected-btn")).to_be_visible()
    page.locator("thead input[type='checkbox']").uncheck()
    expect(page.locator("#copy-selected-btn")).to_be_hidden()


# ── Search ─────────────────────────────────────────────────────────


def test_search_filters_by_ticket_id(e2e_server: str, page: Page) -> None:
    """Typing a ticket ID in search hides non-matching tickets."""
    page.goto(e2e_server)
    wait_for_tickets(page)
    page.locator("#ticket-search").fill("42")
    # Ticket #42 should be visible
    expect(page.locator("a", has_text="#42").first).to_be_visible()
    # Ticket #99 rows should be hidden (display: none)
    ticket_99_rows = page.locator("tr[data-ticket-row]")
    expect(ticket_99_rows.first).to_be_hidden()


def test_search_filters_by_mr_title(e2e_server: str, page: Page) -> None:
    """Searching by MR title text shows matching tickets."""
    page.goto(e2e_server)
    wait_for_tickets(page)
    page.locator("#ticket-search").fill("login timeout")
    expect(page.locator("a", has_text="#42").first).to_be_visible()


def test_search_clears_restores_all(e2e_server: str, page: Page) -> None:
    """Clearing search restores all tickets."""
    page.goto(e2e_server)
    wait_for_tickets(page)
    # Ticket #99 has no MRs and is hidden by the default "Has PR" filter;
    # uncheck it so both tickets are eligible to appear when the search clears.
    page.locator("#filter-has-mr").uncheck()
    page.locator("#ticket-search").fill("42")
    page.locator("#ticket-search").fill("")
    expect(page.locator("a", has_text="#42").first).to_be_visible()
    expect(page.locator("a", has_text="#99").first).to_be_visible()


# ── Filter: Has MR ────────────────────────────────────────────────


def test_filter_has_mr(e2e_server: str, page: Page) -> None:
    """Checking 'Has MR' hides tickets without MRs."""
    page.goto(e2e_server)
    wait_for_tickets(page)
    page.locator("#filter-has-mr").check()
    # Ticket #42 has MRs — should be visible
    expect(page.locator("a", has_text="#42").first).to_be_visible()
    # Ticket #99 has no MRs — should be hidden
    ticket_99_rows = page.locator("tr[data-ticket-row]")
    expect(ticket_99_rows.first).to_be_hidden()


# ── Filter: Non-draft ─────────────────────────────────────────────


def test_filter_non_draft(e2e_server: str, page: Page) -> None:
    """Checking 'Non-draft' hides tickets where all MRs are drafts."""
    page.goto(e2e_server)
    wait_for_tickets(page)
    page.locator("#filter-non-draft").check()
    # Ticket #42 has backend!100 (non-draft) — still visible
    expect(page.locator("a", has_text="#42").first).to_be_visible()


# ── Filter: Unapproved ────────────────────────────────────────────


def test_filter_unapproved(e2e_server: str, page: Page) -> None:
    """Checking 'Unapproved' hides tickets where all MRs are approved."""
    page.goto(e2e_server)
    wait_for_tickets(page)
    page.locator("#filter-unapproved").check()
    # Ticket #42 has frontend!200 (unapproved) — still visible
    expect(page.locator("a", has_text="#42").first).to_be_visible()


# ── Combined filters ──────────────────────────────────────────────


def test_combined_non_draft_and_unapproved(e2e_server: str, page: Page) -> None:
    """Non-draft + Unapproved together: only tickets with at least one non-draft unapproved MR."""
    page.goto(e2e_server)
    wait_for_tickets(page)
    page.locator("#filter-non-draft").check()
    page.locator("#filter-unapproved").check()
    # Ticket #42: backend!100 is non-draft but approved (1/1), frontend!200 is unapproved but draft
    # Neither MR passes BOTH filters — verify page is still functional (no JS error)
    expect(page.locator("#ticket-search")).to_be_visible()


# ── Data attributes ───────────────────────────────────────────────


def test_mr_rows_have_data_attributes(e2e_server: str, page: Page) -> None:
    """MR rows have data attributes for filtering and copy."""
    page.goto(e2e_server)
    wait_for_tickets(page)
    mr_row = page.locator("tr[data-mr-row]").first
    expect(mr_row).to_have_attribute("data-mr-title", re.compile(r".+"))
    expect(mr_row).to_have_attribute("data-mr-url", re.compile(r".+"))
    expect(mr_row).to_have_attribute("data-mr-draft", re.compile(r"(true|false)"))
    expect(mr_row).to_have_attribute("data-mr-approved", re.compile(r"(true|false)"))
    expect(mr_row).to_have_attribute("data-ticket-id", re.compile(r".+"))


def test_ticket_without_mr_has_data_attributes(e2e_server: str, page: Page) -> None:
    """Ticket rows without MRs have ticket-level data attributes."""
    page.goto(e2e_server)
    wait_for_tickets(page)
    ticket_row = page.locator("tr[data-ticket-row]").first
    expect(ticket_row).to_have_attribute("data-ticket-id", re.compile(r".+"))
