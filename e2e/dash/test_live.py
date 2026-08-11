"""The live-work page in a browser: the four panels, and the poll that keeps them fresh.

The Django-level tests assert the read model and the markup. These assert the two
things only a browser can show — that the page is reachable from the nav a user
actually clicks, and that its polled region morphs in place rather than replacing
itself (which is what keeps scroll position stable while an operator is reading it).
"""

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from teatree.core.models.task_attempt import TaskAttempt
from tests.factories import TaskFactory


@pytest.fixture
def running_attempt(request: pytest.FixtureRequest) -> TaskAttempt:
    """One in-flight attempt with a recorded bundle, committed for the server thread."""
    request.getfixturevalue("transactional_db")
    board = request.getfixturevalue("seeded_board")
    task = TaskFactory(ticket=board.building, phase="coding")
    return TaskAttempt.objects.create(
        task=task,
        model="claude-opus-4-8",
        skills_loaded=["t3:code", "t3:rules"],
        agent_session_id="e2e-live-session",
    )


@pytest.mark.usefixtures("running_attempt")
def test_the_nav_reaches_the_live_page_and_it_names_the_running_work(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/board/")
    page.get_by_role("link", name="Live", exact=True).click()

    expect(page.get_by_role("heading", name="Live work", level=1)).to_be_visible()
    expect(page.get_by_role("heading", name="Running")).to_be_visible()
    expect(page.get_by_role("heading", name="Queue")).to_be_visible()
    expect(page.get_by_role("heading", name="Recent outcomes")).to_be_visible()
    # Scoped to the running row: the same ticket also appears in the queue panel,
    # and an unscoped text match would resolve to both.
    running = page.locator("tr[id^='running-']").filter(has_text="build the widget")
    expect(running).to_be_visible()
    expect(running.get_by_text("t3:code", exact=True)).to_be_visible()


@pytest.mark.usefixtures("running_attempt")
def test_the_polled_region_morphs_in_place_rather_than_replacing_itself(live_server: LiveServer, page: Page) -> None:
    """A replaced node loses scroll and drops focus; a morphed one keeps both."""
    page.goto(f"{live_server.url}/dash/live/")
    region = page.locator("#live-work")

    expect(region).to_have_attribute("hx-swap", "morph:innerHTML")
    expect(region).to_have_attribute("hx-sync", "this:drop")

    row = page.locator("tr[id^='running-']").first
    expect(row).to_be_visible()
    row_id = row.get_attribute("id")
    page.wait_for_timeout(6000)
    expect(page.locator(f"#{row_id}")).to_be_visible()


@pytest.mark.usefixtures("seeded_board")
def test_an_idle_factory_says_so_rather_than_rendering_an_empty_table(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/live/")
    expect(page.get_by_text("No attempt is running.")).to_be_visible()
