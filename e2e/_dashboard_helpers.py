"""Shared helpers for dashboard E2E tests.

The dashboard panels render twice on each visit: Django's server-side render
emits the initial HTML, then HTMX immediately re-fetches via the panels'
``hx-trigger="load"`` and replaces the inner HTML with a fresh fetch. A test
that interacts with elements before the HTMX swap completes ends up clicking
DOM nodes that get discarded — typical symptom is a button that should toggle
class ``hidden`` but stays hidden because the change handler ran on a stale,
removed checkbox.

``wait_for_dashboard_idle`` waits for the global signal exposed by
``dashboard.html`` (``body[data-htmx-idle="1"]``) which flips to ``"1"`` only
after at least one HTMX request has fired and every in-flight request has
settled. Use it after ``page.goto(e2e_server)`` and before any interaction.

``wait_for_tickets`` / ``wait_for_sessions`` combine the idle gate with a
panel-specific readiness check. Use these whenever the test will interact
with (click, check, fill) elements inside those panels. Pure-assertion
tests that rely on Playwright's auto-retrying ``expect()`` don't need the
idle gate — assertions retry past the swap on their own.
"""

from playwright.sync_api import Page


def wait_for_dashboard_idle(page: Page) -> None:
    """Block until at least one HTMX request has fired and all are settled."""
    page.locator('body[data-htmx-idle="1"]').wait_for()


def wait_for_tickets(page: Page) -> None:
    """Wait for HTMX idle then the first ticket MR row to attach."""
    wait_for_dashboard_idle(page)
    page.locator("tr[data-mr-row]").first.wait_for(state="attached")


def wait_for_sessions(page: Page) -> None:
    """Wait for HTMX idle then the unified sessions grid to be visible."""
    wait_for_dashboard_idle(page)
    page.locator("#unified-sessions-grid").wait_for(state="visible")
