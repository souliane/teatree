"""The preset editor's meta form as a user-visible round trip.

Saving a description re-renders the open card carrying the new text; the paired
absence guard is the availability-pin select, which no longer exists.
"""

import pytest
from playwright.sync_api import Locator, Page, expect
from pytest_django.live_server_helper import LiveServer

from teatree.loops.preset_seed import seed_default_presets_and_schedules

# The create-preset form carries its own `description` field labelled "new preset
# description", which `get_by_label("preset description")` matches by substring — so
# every field here is scoped to the meta form, as the loops specs scope theirs.
_META_FORM = 'form[action*="/presets/meta/"]'


def _description_field(page: Page) -> Locator:
    return page.locator(f'{_META_FORM} input[name="description"]')


@pytest.fixture
def seeded_presets(request: pytest.FixtureRequest) -> None:
    """The 7 shipped presets, committed so the ``live_server`` thread sees them."""
    request.getfixturevalue("transactional_db")
    seed_default_presets_and_schedules()


@pytest.mark.usefixtures("seeded_presets")
def test_saving_a_description_round_trips_on_the_open_card(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/presets/?preset=engaged")
    _description_field(page).fill("nine to five, hands on deck")

    page.get_by_role("button", name="save description").click()

    expect(_description_field(page)).to_have_value("nine to five, hands on deck")


@pytest.mark.usefixtures("seeded_presets")
def test_the_meta_form_offers_no_availability_pin(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/presets/?preset=engaged")

    # The description field anchors the absence assertions to a meta form that really
    # rendered — a scoped absence alone would pass against a form that never existed.
    expect(_description_field(page)).to_have_count(1)
    expect(page.locator(f'{_META_FORM} select[name="availability_pin"]')).to_have_count(0)
    expect(page.get_by_text("pins availability")).to_have_count(0)
