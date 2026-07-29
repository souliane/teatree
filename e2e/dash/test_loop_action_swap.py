"""A loop-action POST must SWAP the surface, not merely return the right bytes.

The Django test client structurally cannot see this class of defect: it posts a
dict, so it never serializes a form and never performs a swap. It therefore passes
on markup that no browser can act on — which is exactly how a broken submitter and
then a non-landing swap both reached CI green locally. The browser is the only
instrument, so the failure message carries htmx's own event log rather than leaving
the next reader to guess which half broke.
"""

import logging

import pytest
from playwright.sync_api import ConsoleMessage, Page, expect
from pytest_django.live_server_helper import LiveServer

_LOG = logging.getLogger(__name__)

_HTMX_EVENTS = (
    "htmx:configRequest",
    "htmx:beforeRequest",
    "htmx:beforeSwap",
    "htmx:afterSwap",
    "htmx:afterSettle",
    "htmx:targetError",
    "htmx:swapError",
    "htmx:responseError",
    "htmx:sendError",
)

_RECORD_HTMX_LIFECYCLE = """
(names) => {
    window.__probe = {
        events: [],
        ext: document.body.getAttribute("hx-ext"),
        idiomorph: typeof window.Idiomorph,
        htmx: typeof window.htmx,
    };
    for (const name of names) {
        document.body.addEventListener(name, (evt) => {
            const d = evt.detail || {};
            window.__probe.events.push(
                name +
                " target=" + (d.target ? (d.target.id || d.target.tagName) : "-") +
                " status=" + (d.xhr ? d.xhr.status : "-") +
                " shouldSwap=" + d.shouldSwap +
                " swapStyle=" + (d.swapSpec ? d.swapSpec.swapStyle : "-")
            );
        });
    }
}
"""


def _loop_row(page: Page):  # noqa: ANN202 — Playwright's Locator, inferred
    return page.locator("tr").filter(has_text="e2e_loop")


@pytest.mark.usefixtures("seeded_board")
def test_a_loop_action_swaps_the_surface(live_server: LiveServer, page: Page) -> None:
    console: list[str] = []

    def _record(message: ConsoleMessage) -> None:
        if message.type == "error":
            console.append(f"console.error: {message.text}")

    page.on("pageerror", lambda exc: console.append(f"pageerror: {exc}"))
    page.on("console", _record)

    page.goto(f"{live_server.url}/dash/loops/")
    page.evaluate(_RECORD_HTMX_LIFECYCLE, list(_HTMX_EVENTS))

    _loop_row(page).get_by_role("button", name="pause").click()
    page.wait_for_timeout(2000)

    probe = page.evaluate("() => window.__probe")
    _LOG.warning(
        "swap probe: htmx=%s Idiomorph=%s hx-ext=%r\nevents:\n%s\nconsole:\n%s\nrow:\n%s",
        probe["htmx"],
        probe["idiomorph"],
        probe["ext"],
        "\n".join(probe["events"]) or "(none)",
        "\n".join(console) or "(none)",
        _loop_row(page).inner_html()[:900],
    )

    expect(_loop_row(page).get_by_role("button", name="resume")).to_be_visible()
