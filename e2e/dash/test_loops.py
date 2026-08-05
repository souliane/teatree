"""Loop control as a user-visible round trip (#3162), and the starved chip (#4185).

Pause flips the row to held and swaps the verb to resume, resume restores, and
the fail-open gate refuses without the exact confirm phrase. A loop the preset admits
with no timer chain behind it shows a ``starved`` chip until the chain is headed.
"""

from http import HTTPStatus

import pytest
from playwright.sync_api import Locator, Page, expect
from pytest_django.live_server_helper import LiveServer

from teatree.core.models.loop import Loop
from teatree.core.models.loop_preset import Mode, ModeOverride
from teatree.loops.registry import iter_loops
from teatree.loops.timer_reconciler import ensure_loop_timers


def _loop_row(page: Page, name: str = "e2e_loop") -> Locator:
    return page.locator("tr").filter(has_text=name)


@pytest.mark.usefixtures("seeded_board")
def test_pause_flips_to_held_and_swaps_the_verb(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/loops/")
    _loop_row(page).get_by_role("button", name="pause").click()
    row = _loop_row(page)
    expect(row).to_contain_text("held")
    expect(row.get_by_role("button", name="resume")).to_be_visible()
    expect(row.get_by_role("button", name="pause")).to_have_count(0)


@pytest.mark.usefixtures("seeded_board")
def test_resume_restores_running(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/loops/")
    _loop_row(page).get_by_role("button", name="pause").click()
    _loop_row(page).get_by_role("button", name="resume").click()
    row = _loop_row(page)
    expect(row).to_contain_text("running")
    expect(row.get_by_role("button", name="pause")).to_be_visible()


@pytest.mark.usefixtures("seeded_board")
def test_gate_toggle_refuses_without_the_confirm_phrase(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/loops/")
    # The matcher requires a 400 from the gate endpoint — the phrase-less POST is
    # refused, which is the behavioral proof (a timeout here means it was NOT refused).
    with page.expect_response(
        lambda r: r.url.endswith("/loops/gate/") and r.status == HTTPStatus.BAD_REQUEST,
    ):
        page.get_by_role("button", name="turn ON").click()


@pytest.mark.usefixtures("seeded_board")
def test_gate_toggle_enables_with_the_confirm_phrase(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/loops/")
    # The loop kill-switch (#3623) adds a second `input[name="confirm"]` on this page,
    # so scope the fill to the fail-open gate form to keep the locator unambiguous.
    page.locator('form[action*="/loops/gate/"] input[name="confirm"]').fill("fail-open")
    page.get_by_role("button", name="turn ON").click()
    # Now on — the loops page offers the restore button and the fail-open state.
    expect(page.get_by_role("button", name="turn OFF")).to_be_visible()


@pytest.fixture
def starved_loop(request: pytest.FixtureRequest) -> str:
    """A registered live-tick loop the preset forces ON, with no timer chain at all.

    Committed via ``transactional_db`` so the ``live_server`` thread serving the browser
    sees the rows. The name comes from the registry because the loops panel is
    registry-keyed — a row naming no registered mini-loop never renders.
    """
    request.getfixturevalue("transactional_db")
    name = iter_loops()[0].name
    Loop.objects.filter(name=name).delete()
    Loop.objects.create(name=name, script=f"{name}/run.py", delay_seconds=60, enabled=False, last_run_at=None)
    Mode.objects.create(name="e2e-forced-on", entries={name: True})
    ModeOverride.objects.set_override("e2e-forced-on")
    return name


def test_a_starved_loop_shows_a_chip_that_clears_once_the_chain_is_headed(
    live_server: LiveServer, page: Page, starved_loop: str
) -> None:
    page.goto(f"{live_server.url}/dash/live/")
    row = page.locator(f"#live-loop-{starved_loop}")
    expect(row).to_contain_text("starved")

    ensure_loop_timers()

    page.reload()
    row = page.locator(f"#live-loop-{starved_loop}")
    expect(row).not_to_contain_text("starved")
