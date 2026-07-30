"""A 5s morph poll must not overwrite the cadence field being typed into.

``#loops-table`` re-renders every 5 seconds with ``hx-swap="morph:innerHTML"`` and
contains the per-loop cadence inputs. Idiomorph assigns the DOM ``value`` PROPERTY
during a morph — bypassing the dirty-value flag — and its ``ignoreActiveValue``
default is falsy, so an unguarded poll resets whatever field has focus mid-edit. The
shell flips that default; only a real browser can show that the guard holds.
"""

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from teatree.core.models import Loop

_POLL_MS = 5_000


@pytest.mark.usefixtures("transactional_db")
def test_a_poll_does_not_clobber_the_cadence_field_being_typed_into(live_server: LiveServer, page: Page) -> None:
    Loop.objects.create(name="demo-cadence", delay_seconds=60, script="run.py", enabled=True)
    page.goto(f"{live_server.url}/dash/loops/")

    field = page.get_by_label("demo-cadence interval seconds")
    field.click()
    field.fill("900")

    page.wait_for_timeout(_POLL_MS * 2 + 500)

    expect(field).to_be_focused()
    expect(field).to_have_value("900")


@pytest.mark.usefixtures("transactional_db")
def test_an_unfocused_row_still_refreshes_from_the_poll(live_server: LiveServer, page: Page) -> None:
    """The guard must be scoped to the ACTIVE element, not a blanket freeze of the table."""
    loop = Loop.objects.create(name="demo-refresh", delay_seconds=60, script="run.py", enabled=True)
    page.goto(f"{live_server.url}/dash/loops/")

    loop.delay_seconds = 120
    loop.save(update_fields=["delay_seconds"])

    page.wait_for_timeout(_POLL_MS + 500)
    expect(page.get_by_label("demo-refresh interval seconds")).to_have_value("120")
