"""E2E tests for dashboard fixes (issue #62 + bug-hunt #455).

Covers: vendored JS, logo, terminal options, overlay dropdown, task launch,
plus regressions for the bug-hunt 2026-04-25 findings.
"""

import re

from playwright.sync_api import Page, expect

# ── Vendored JS (no CDN dependency) ────────────────────────────────


def test_htmx_loaded_from_static(e2e_server: str, page: Page) -> None:
    """Htmx is served from local static files, not CDN."""
    responses: list[str] = []
    page.on("response", lambda resp: responses.append(resp.url))
    page.goto(e2e_server)
    htmx_urls = [u for u in responses if "htmx" in u]
    assert htmx_urls, "No htmx script loaded"
    for url in htmx_urls:
        assert "unpkg.com" not in url, f"htmx loaded from CDN: {url}"
        assert "cdn." not in url, f"htmx loaded from CDN: {url}"


def test_sse_js_loaded_from_static(e2e_server: str, page: Page) -> None:
    """SSE extension is served from local static files, not CDN."""
    responses: list[str] = []
    page.on("response", lambda resp: responses.append(resp.url))
    page.goto(e2e_server)
    sse_urls = [u for u in responses if "sse" in u]
    assert sse_urls, "No SSE script loaded"
    for url in sse_urls:
        assert "unpkg.com" not in url, f"SSE loaded from CDN: {url}"


def test_no_js_load_failures(e2e_server: str, page: Page) -> None:
    """No JavaScript files fail to load (no 404s for static assets)."""
    failed: list[str] = []
    page.on("requestfailed", lambda req: failed.append(f"{req.url} - {req.failure}"))
    page.goto(e2e_server)
    page.wait_for_timeout(1000)
    js_failures = [f for f in failed if ".js" in f]
    assert not js_failures, f"JS load failures: {js_failures}"


# ── Logo ───────────────────────────────────────────────────────────


def test_logo_is_visible(e2e_server: str, page: Page) -> None:
    """Dashboard logo image loads successfully."""
    page.goto(e2e_server)
    logo = page.locator("#dashboard-logo")
    expect(logo).to_be_visible()
    # Verify image actually loaded (naturalWidth > 0)
    natural_width = logo.evaluate("el => el.naturalWidth")
    assert natural_width > 0, "Logo image failed to load (naturalWidth=0)"


def test_logo_uses_jpg(e2e_server: str, page: Page) -> None:
    """Logo src points to the JPG file, not the old SVG placeholder."""
    page.goto(e2e_server)
    logo = page.locator("#dashboard-logo")
    src = logo.get_attribute("src") or ""
    assert "teatree-logo.jpg" in src, f"Expected JPG logo, got: {src}"


# ── Terminal options ───────────────────────────────────────────────


def test_terminal_options_menu_opens(e2e_server: str, page: Page) -> None:
    """Clicking the dropdown arrow on Terminal button opens the options menu."""
    page.goto(e2e_server)
    arrow = page.locator(".split-btn .split-arrow").first
    arrow.click()
    menu = page.locator(".split-menu").first
    expect(menu).to_be_visible()
    expect(menu).to_contain_text("New window")


def test_terminal_option_persists_to_localstorage(e2e_server: str, page: Page) -> None:
    """Selecting a terminal option saves it to localStorage."""
    page.goto(e2e_server)
    arrow = page.locator(".split-btn .split-arrow").first
    arrow.click()
    page.locator(".split-menu button[data-value='ttyd']").first.click()
    stored = page.evaluate("localStorage.getItem('teatree_terminal_mode')")
    assert stored == "ttyd"


def test_terminal_option_preselected_on_load(e2e_server: str, page: Page) -> None:
    """Terminal option from localStorage is visually marked as active on page load."""
    page.goto(e2e_server)
    # Set a preference
    page.evaluate("localStorage.setItem('teatree_terminal_mode', 'ttyd')")
    page.reload()
    page.wait_for_timeout(500)
    # The ttyd button should have the 'active' class
    active_btn = page.locator(".split-menu button[data-value='ttyd'].active").first
    expect(active_btn).to_be_attached()


# ── Overlay dropdown ───────────────────────────────────────────────


def test_overlay_selector_has_all_overlays_option(e2e_server: str, page: Page) -> None:
    """When overlay selector exists, it includes an 'All overlays' option."""
    page.goto(e2e_server)
    page.wait_for_timeout(500)
    selector = page.locator("#overlay-selector")
    if selector.count() > 0:
        expect(selector).to_be_visible()
        expect(selector.locator("option[value='']")).to_contain_text("All overlays")


# ── Task launch passes terminal mode ──────────────────────────────


def test_launch_button_has_terminal_params_in_markup(e2e_server: str, page: Page) -> None:
    """Interactive queue Launch button includes hx-on attribute that sends terminal params."""
    page.goto(e2e_server)
    launch_btn = page.locator("button", has_text="Launch").first
    if launch_btn.count() == 0:
        # No interactive tasks in queue — create one first
        page.locator("button", has_text="Interactive").first.click()
        page.wait_for_timeout(1000)
        page.reload()
        page.wait_for_timeout(1000)
    launch_btn = page.locator("button", has_text="Launch").first
    if launch_btn.count() > 0:
        before_request = launch_btn.get_attribute("hx-on::before-request") or ""
        assert "terminal_mode" in before_request, f"Launch button missing terminal_mode injection: {before_request}"


# ── No console errors ──────────────────────────────────────────────


def test_static_assets_return_200(e2e_server: str, page: Page) -> None:
    """Vendored JS files return HTTP 200."""
    for path in ["static/teatree/js/htmx-2.0.4.min.js", "static/teatree/js/htmx-ext-sse-2.2.4.js"]:
        resp = page.request.get(f"{e2e_server}/{path}")
        assert resp.status == 200, f"{path} returned {resp.status}"  # noqa: PLR2004


# ── Bug hunt 2026-04-25 (#455) ───────────────────────────────────────


def test_no_sse_listener_pageerror_on_load(e2e_server: str, page: Page) -> None:
    """The SSE listeners do not throw on initial dashboard load (#455 §1).

    Regression for the SSE listeners being registered in `<head>` before
    `<body>` parsed — `document.body.addEventListener(...)` threw
    `TypeError: Cannot read properties of null (reading 'addEventListener')`
    every load, halting the inline script. We narrow the assertion to that
    specific failure mode so the test stays focused on §1; unrelated 3rd-party
    errors (e.g. mermaid on diagram-less pages) are tracked separately.
    """
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(e2e_server)
    page.wait_for_timeout(800)
    sse_errors = [e for e in errors if "addEventListener" in e]
    assert sse_errors == [], f"SSE listener errors on dashboard load: {sse_errors}"


def test_sse_status_indicator_present(e2e_server: str, page: Page) -> None:
    """The `#sse-status` indicator that the SSE listeners reference must exist (#455 §3)."""
    page.goto(e2e_server)
    expect(page.locator("#sse-status")).to_be_attached()


def test_task_detail_url_404s_without_htmx(e2e_server: str, page: Page) -> None:
    """Direct browser nav to the task detail partial returns 404 (#455 §6)."""
    response = page.goto(f"{e2e_server}/tasks/1/detail/")
    assert response is not None
    assert response.status == 404  # noqa: PLR2004


def test_ticket_lifecycle_url_404s_without_htmx(e2e_server: str, page: Page) -> None:
    """Direct browser nav to the lifecycle partial returns 404 (#455 §6)."""
    response = page.goto(f"{e2e_server}/tickets/1/lifecycle/")
    assert response is not None
    assert response.status == 404  # noqa: PLR2004


def test_task_graph_url_404s_without_htmx(e2e_server: str, page: Page) -> None:
    """Direct browser nav to the task-graph partial returns 404 (#455 §6)."""
    response = page.goto(f"{e2e_server}/tickets/1/task-graph/")
    assert response is not None
    assert response.status == 404  # noqa: PLR2004


def test_active_worktrees_kpi_matches_panel(e2e_server: str, page: Page) -> None:
    """KPI count must equal the worktrees panel row count (#455 §4).

    The worktrees panel is HTMX-loaded only on demand (not from the dashboard
    homepage), so we hit it as an HTMX request and parse the markup directly.
    Both the KPI and the panel must derive from the same builder
    (`build_worktree_rows`); this asserts they actually agree at a fixed point
    in time.
    """
    page.goto(e2e_server)
    kpi_card = page.locator("article", has_text="Active Worktrees")
    kpi_text = kpi_card.locator("div").filter(has_text=re.compile(r"^\d+$")).first.inner_text().strip()
    kpi_count = int(kpi_text) if kpi_text.isdigit() else 0

    panel_html = page.request.get(
        f"{e2e_server}/dashboard/panels/worktrees/",
        headers={"HX-Request": "true"},
    ).text()
    # Worktree rows live in <tbody>; header lives in <thead>. Match the tbody section.
    tbody_match = re.search(r"<tbody[^>]*>(.*?)</tbody>", panel_html, re.DOTALL)
    panel_rows = tbody_match.group(1).count("<tr") if tbody_match else 0

    assert kpi_count == panel_rows, f"KPI says {kpi_count} active worktrees, panel shows {panel_rows} rows"
