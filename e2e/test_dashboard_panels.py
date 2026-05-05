"""E2E coverage for dashboard panels not previously asserted.

Functional asserts (text/elements present), no snapshot diffing — see
[#19](https://github.com/souliane/teatree/issues/19) for context. Snapshot
tests against these panels are platform-sensitive and were the reason the
original ``test_action_required_panel`` was skipped on every run.

Worktrees and Activity panels aren't rendered as standalone ``<details>``
blocks on the dashboard home page (only Automation, Action Required,
Sessions, In-Flight Tickets, Pending Reviews are). For those we drive the
HTMX endpoint directly with the ``HX-Request`` header — that's the
contract real HTMX clients hit at runtime.
"""

from playwright.sync_api import APIResponse, Page, expect


def _fetch_panel(page: Page, e2e_server: str, panel: str) -> APIResponse:
    """GET ``/dashboard/panels/<panel>/`` with HTMX header. Returns Playwright APIResponse."""
    return page.request.get(
        f"{e2e_server}/dashboard/panels/{panel}/",
        headers={"HX-Request": "true"},
    )


def test_action_required_panel(e2e_server: str, page: Page) -> None:
    """Section renders and surfaces the seeded interactive task as an action item.

    Replaces the platform-sensitive snapshot test that was skipped on every
    run. The seeded ticket has one ``execution_target=interactive`` Task in
    ``PENDING`` status — ``build_action_required`` turns that into an
    ``"interactive_task"`` row labeled with the ticket number + reason.
    """
    page.goto(e2e_server)
    expect(page.locator("h2", has_text="Action Required")).to_be_visible()
    # The seeded interactive task carries this exact ``execution_reason``.
    # Rendered as the right-hand "detail" span on the action item card.
    expect(page.locator("body")).to_contain_text("Needs manual verification")
    expect(page.locator("span.pill", has_text="Interactive").first).to_be_visible()


def test_worktrees_panel(e2e_server: str, page: Page) -> None:
    """Worktrees panel returns 200 and renders the seeded worktree rows.

    The panel isn't on the home dashboard (only consumed via HTMX); we hit
    the endpoint directly and parse the response body.
    """
    response = _fetch_panel(page, e2e_server, "worktrees")
    assert response.status == 200, f"got {response.status}: {response.text()[:200]}"  # noqa: PLR2004
    body = response.text()
    # Section column headers from ``dashboard_worktrees.html``.
    assert "Ticket" in body
    assert "Branch" in body
    assert "Database" in body
    # Both seeded worktrees are visible (one provisioned, one ready).
    assert "feat-42" in body
    assert "wt_42_demo" in body
    assert "wt_42_demo_fe" in body


def test_pending_reviews_panel(e2e_server: str, page: Page) -> None:
    """Pending Reviews section renders the seeded cache rows.

    Issue #19 calls this panel ``review_comments``; the actual builder key
    (and template) is ``pending_reviews``. ``conftest._seed_data`` primes
    the ``PENDING_REVIEWS_CACHE_KEY`` Django cache the builder reads from
    — using FileBasedCache so the test process's writes reach the live
    ASGI subprocess.
    """
    page.goto(e2e_server)
    expect(page.locator("h2", has_text="Pending Reviews")).to_be_visible()
    # Two seeded entries: one non-draft, one draft.
    expect(page.locator("body")).to_contain_text("backend\xa0#505")
    expect(page.locator("body")).to_contain_text("frontend\xa0#612")
    expect(page.locator("body")).to_contain_text("nightly export pipeline")
    # Draft pill on the second row.
    expect(page.locator("span.pill", has_text="Draft").first).to_be_visible()


def test_activity_panel(e2e_server: str, page: Page) -> None:
    """Activity panel returns 200 and renders the seeded TaskAttempt row.

    The headless task seeded in conftest runs synchronously under
    ImmediateBackend, producing a TaskAttempt that ``build_recent_activity``
    surfaces as an activity card. Real attempts fail in CI (no ``claude``
    binary) — we accept any of the rendered exit_code branches.
    """
    response = _fetch_panel(page, e2e_server, "activity")
    assert response.status == 200, f"got {response.status}: {response.text()[:200]}"  # noqa: PLR2004
    body = response.text()
    # The seeded ticket renders as ``#42`` (Ticket._display_id).
    assert "#42" in body
    # Headless task phase from seed data.
    assert "reviewing" in body
    # Any of the three exit_code badges from ``dashboard_activity.html``.
    assert any(badge in body for badge in ("Success", "Failed", "Unknown")), (
        f"expected an exit-code badge in the rendered card; got body[:400]={body[:400]!r}"
    )
