"""The one settings page: every key visible under a group, the readouts absorbed, config retired.

The browser-level counterpart to ``tests/teatree_dash/views/test_settings.py`` — it proves
what the test client cannot: that the nav really lost its Config entry, that a bookmark on
the old URL lands here, and that a secret's value is absent from what the browser received.

Every heading locator is ``exact=True``. Playwright matches an accessible name by
case-insensitive SUBSTRING by default, and this page legitimately carries both a
``Credentials`` readout and a ``Loop kill switches & credentials`` settings group — two
different questions about the same coordinates. A substring locator cannot tell them
apart and resolves to two elements; the exact one names which panel is meant.
"""

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from teatree.config.schema import TeatreeSettingsSchema
from teatree.core.models.config_setting import ConfigSetting


@pytest.mark.usefixtures("seeded_board")
def test_every_schema_key_is_reachable_under_a_group_heading(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/settings/")
    expect(page.get_by_role("heading", name="Quality gates", exact=True)).to_be_visible()
    # The two keys the retired band classifier dropped, now each under its own group.
    expect(page.locator("#setting-bulk_close_threshold")).to_be_attached()
    expect(page.locator("#setting-disk_warn_free_gb")).to_be_attached()
    expect(page.locator('tr[id^="setting-"]')).to_have_count(len(TeatreeSettingsSchema.model_fields))


@pytest.mark.usefixtures("seeded_board")
def test_the_page_absorbs_the_retired_config_readouts(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/settings/")
    expect(page.get_by_role("heading", name="Model & reasoning effort", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Credentials", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Self-repairs", exact=True)).to_be_visible()


@pytest.mark.usefixtures("seeded_board")
def test_the_old_config_url_lands_on_the_settings_page_with_no_config_nav_entry(
    live_server: LiveServer, page: Page
) -> None:
    page.goto(f"{live_server.url}/dash/config/")
    expect(page).to_have_url(f"{live_server.url}/dash/settings/")
    expect(page.get_by_role("link", name="Config", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Settings", exact=True)).to_be_visible()


@pytest.mark.usefixtures("seeded_board")
def test_a_configured_secret_never_reaches_the_browser(live_server: LiveServer, page: Page) -> None:
    ConfigSetting.objects.set_value("banned_terms", ["sentinel-marker-xyz"])
    page.goto(f"{live_server.url}/dash/settings/")
    expect(page.locator("#setting-banned_terms")).to_contain_text("***")
    expect(page.locator("body")).not_to_contain_text("sentinel-marker-xyz")
