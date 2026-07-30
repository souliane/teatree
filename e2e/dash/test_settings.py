"""The settings page: sections on the left, one section's rows on the right.

The browser-level counterpart to ``tests/teatree_dash/views/test_settings.py`` — it proves
what the test client cannot: that the nav really lost its Config entry, that a bookmark on
the old URL lands here, that a secret's value is absent from what the browser received, and
that selecting a section swaps ONLY the detail pane while the rest of the document stands.

Every heading locator is ``exact=True``. Playwright matches an accessible name by
case-insensitive SUBSTRING by default, and this page legitimately carries both a
``Credentials`` readout and a ``Kill switches & credentials`` settings section — two
different questions about the same coordinates. A substring locator cannot tell them
apart and resolves to two elements; the exact one names which panel is meant.
"""

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from teatree.core.models.config_setting import ConfigSetting
from teatree.dash.settings_editor import SettingsSection, build_settings_sections

#: The section a browser assertion navigates to — a leaf several levels down, so the pane
#: swap is exercised against real nesting rather than a top-level group.
_MERGE_AND_DONE = ("Gates", "Quality", "Merge & done")

#: The viewport the before-measurements were taken in, and the height ceiling this change
#: has to stay under. Before: 1,925px wide inside 1,600px, and 14,212px tall.
_VIEWPORT = {"width": 1600, "height": 1000}
_MAX_PAGE_HEIGHT_PX = 4000


def _section(path: tuple[str, ...]) -> SettingsSection:
    return next(section for section in build_settings_sections() if section.path == path)


@pytest.mark.usefixtures("seeded_board")
def test_the_nav_lists_every_section_and_the_pane_shows_one(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/settings/")
    nav = page.get_by_role("navigation", name="Settings sections")
    expect(nav).to_be_visible()
    expect(nav.get_by_role("button")).to_have_count(len(build_settings_sections()))
    # The pane holds exactly the first section's rows, not the whole schema.
    first = build_settings_sections()[0]
    expect(page.locator('tr[id^="setting-"]')).to_have_count(first.key_count)


@pytest.mark.usefixtures("seeded_board")
def test_selecting_a_section_swaps_only_the_detail_pane(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/settings/")
    section = _section(_MERGE_AND_DONE)
    page.get_by_role("button", name=section.label, exact=False).first.click()
    pane = page.locator("#settings-pane")
    expect(pane.locator(f'section[data-group-slug="{section.slug}"]')).to_be_visible()
    expect(page.locator('tr[id^="setting-"]')).to_have_count(section.key_count)
    # The rest of the document stood: the nav and the readouts were never re-rendered.
    expect(page.get_by_role("navigation", name="Settings sections")).to_be_visible()
    expect(page.get_by_role("heading", name="Credentials", exact=True)).to_be_visible()


@pytest.mark.usefixtures("seeded_board")
def test_every_section_is_reachable_and_renders_its_own_rows(live_server: LiveServer, page: Page) -> None:
    # The never-drop guarantee at browser level: each section opens a pane carrying exactly
    # the keys the nav counted for it.
    for section in build_settings_sections():
        page.goto(f"{live_server.url}/dash/settings/?section={section.slug}")
        expect(page.locator(f'section[data-group-slug="{section.slug}"]')).to_be_visible()
        expect(page.locator('tr[id^="setting-"]')).to_have_count(section.key_count)


@pytest.mark.usefixtures("seeded_board")
def test_a_row_edit_swaps_that_row_alone_and_never_moves_the_page(live_server: LiveServer, page: Page) -> None:
    section = _section(("Agents", "Mode & harness"))
    page.goto(f"{live_server.url}/dash/settings/?section={section.slug}")
    row = page.locator("#setting-mode")
    row.get_by_placeholder("JSON value").fill('"auto"')
    row.get_by_role("button", name="Save").click()
    expect(page.locator("#setting-mode")).to_contain_text("auto")
    expect(page.locator("#setting-mode")).to_contain_text("DB global scope")
    expect(page.locator('tr[id^="setting-"]')).to_have_count(section.key_count)


@pytest.mark.usefixtures("seeded_board")
def test_the_page_carries_one_csrf_token_and_no_hidden_row_fields(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/settings/")
    expect(page.locator('input[name="csrfmiddlewaretoken"]')).to_have_count(1)
    expect(page.locator('#settings-pane input[type="hidden"]')).to_have_count(0)


@pytest.mark.usefixtures("seeded_board")
def test_the_page_fits_the_viewport_and_is_a_fraction_of_its_old_height(live_server: LiveServer, page: Page) -> None:
    # A failing wait_for_function raises with the live measurement, so the two conditions
    # read as assertions without reaching for `assert` (banned in this lane).
    page.set_viewport_size(_VIEWPORT)
    page.goto(f"{live_server.url}/dash/settings/")
    page.wait_for_function(f"() => document.documentElement.scrollWidth <= {_VIEWPORT['width']}")
    page.wait_for_function(f"() => document.documentElement.scrollHeight < {_MAX_PAGE_HEIGHT_PX}")


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
    section = _section(("Registries", "Term scanning, agent tables & cold reads"))
    page.goto(f"{live_server.url}/dash/settings/?section={section.slug}")
    expect(page.locator("#setting-banned_terms")).to_contain_text("***")
    expect(page.locator("body")).not_to_contain_text("sentinel-marker-xyz")


@pytest.mark.usefixtures("seeded_board")
def test_the_import_control_takes_a_file_not_a_textarea(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/settings/")
    expect(page.get_by_label("TOML file to import")).to_be_visible()
    expect(page.locator("textarea")).to_have_count(0)


@pytest.mark.usefixtures("seeded_board")
def test_the_export_control_offers_both_filters_unticked(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/settings/")
    for label in ("Export default keys only", "Include values that are the same as default"):
        checkbox = page.get_by_label(label)
        expect(checkbox).to_be_visible()
        expect(checkbox).not_to_be_checked()
