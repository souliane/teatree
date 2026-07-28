"""The one settings page: every key visible under a group, the readouts absorbed, config retired.

The browser-level counterpart to ``tests/teatree_dash/views/test_settings.py`` — it proves
what the test client cannot: that the nav really lost its Config entry, that a bookmark on
the old URL lands here, and that a secret's value is absent from what the browser received.

Every heading locator is ``exact=True``. Playwright matches an accessible name by
case-insensitive SUBSTRING by default, and this page legitimately carries both a
``Credentials`` readout and a ``Kill switches & credentials`` settings group — two
different questions about the same coordinates. A substring locator cannot tell them
apart and resolves to two elements; the exact one names which panel is meant.

The grouping is a NESTED hierarchy, so the heading locators carry ``level`` too. A
heading's level is an accessibility-tree property the browser computes; the test-client
counterpart can only regex the ``<h3>`` tag it was handed. That is the whole reason the
nesting is asserted here as well as there — flatten the tree and every group collapses
to one level, which a tag regex and an a11y query fail in different ways.
"""

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from teatree.config.schema import TeatreeSettingsSchema
from teatree.core.models.config_setting import ConfigSetting


@pytest.mark.usefixtures("seeded_board")
def test_every_schema_key_is_reachable_under_a_group_heading(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/settings/")
    expect(page.get_by_role("heading", name="Gates", exact=True, level=2)).to_be_visible()
    # The two keys the retired band classifier dropped, now each under its own group.
    expect(page.locator("#setting-bulk_close_threshold")).to_be_attached()
    expect(page.locator("#setting-disk_warn_free_gb")).to_be_attached()
    expect(page.locator('tr[id^="setting-"]')).to_have_count(len(TeatreeSettingsSchema.model_fields))


@pytest.mark.usefixtures("seeded_board")
def test_a_group_nests_inside_its_parent_under_a_deeper_heading(live_server: LiveServer, page: Page) -> None:
    # Nesting has to be real structure a reader can see, not a flat list of dotted labels:
    # the child's section sits INSIDE the parent's, under a heading one level deeper, and
    # the leaf — not the parent — is what carries the rows.
    page.goto(f"{live_server.url}/dash/settings/")
    quality = page.locator('section[data-group-path="Gates / Quality"]')
    expect(quality).to_be_visible()
    expect(quality.get_by_role("heading", name="Quality", exact=True, level=3)).to_be_visible()
    leaf = quality.locator('section[data-group-path="Gates / Quality / Merge & done"]')
    expect(leaf.get_by_role("heading", name="Merge & done", exact=True, level=4)).to_be_visible()
    expect(leaf.locator('tr[id^="setting-"]').first).to_be_visible()


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
