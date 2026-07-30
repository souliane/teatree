"""Loop control as a user-visible round trip (#3162).

Pause flips the row to held and swaps the verb to resume, resume restores, and
the fail-open gate refuses without the exact confirm phrase.
"""

from http import HTTPStatus

import pytest
from playwright.sync_api import Locator, Page, expect
from pytest_django.live_server_helper import LiveServer


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
