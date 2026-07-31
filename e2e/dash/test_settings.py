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
def test_a_click_to_edit_persists_with_no_save_button(live_server: LiveServer, page: Page) -> None:
    # `mode` is a Literal, so its cell is a select and picking an option IS the write. There
    # is no Save anywhere on the page — that is the whole point of editing in place.
    section = _section(("Agents", "Mode & harness"))
    page.goto(f"{live_server.url}/dash/settings/?section={section.slug}")
    expect(page.get_by_role("button", name="Save")).to_have_count(0)
    page.get_by_label("mode in global").select_option('"interactive"')
    expect(page.locator("#setting-mode")).to_contain_text("differs from default")
    expect(page.locator("#setting-mode")).to_contain_text("DB global scope")
    # It PERSISTED rather than only re-rendering: a fresh load shows the same value.
    page.goto(f"{live_server.url}/dash/settings/?section={section.slug}")
    expect(page.get_by_label("mode in global")).to_have_value('"interactive"')
    expect(page.locator('tr[id^="setting-"]')).to_have_count(section.key_count)


@pytest.mark.usefixtures("seeded_board")
def test_a_refused_edit_says_why_and_leaves_no_stale_text_looking_saved(live_server: LiveServer, page: Page) -> None:
    # `merge_wip` is an int, so its cell is free text and a non-JSON value reaches the
    # validator. The refusal has to be VISIBLE in the row, and the cell has to come back
    # holding what is actually stored rather than the text that failed.
    section = _section(("Loops", "Cadence & throughput"))
    page.goto(f"{live_server.url}/dash/settings/?section={section.slug}")
    cell = page.get_by_label("merge_wip in global")
    cell.fill("not-json")
    cell.blur()
    expect(page.locator("#setting-merge_wip")).to_contain_text("invalid JSON value")
    expect(page.get_by_label("merge_wip in global")).not_to_have_value("not-json")


@pytest.mark.usefixtures("seeded_board")
def test_one_row_carries_every_scope_and_the_nav_counts_what_drifted(live_server: LiveServer, page: Page) -> None:
    ConfigSetting.objects.set_value("merge_wip", 4)
    ConfigSetting.objects.set_value("merge_wip", 7, scope="demo-overlay")
    section = _section(("Loops", "Cadence & throughput"))
    page.goto(f"{live_server.url}/dash/settings/?section={section.slug}")
    # ONE row, however many scopes hold a row for it — and a column for each.
    expect(page.locator("#setting-merge_wip")).to_have_count(1)
    expect(page.get_by_role("columnheader", name="global", exact=True)).to_be_visible()
    expect(page.get_by_role("columnheader", name="demo-overlay", exact=True)).to_be_visible()
    # The setting drifted in two scopes and its own nav entry counts it ONCE.
    nav_item = page.locator(".settings-nav-item", has_text=section.label).first
    expect(nav_item.locator(".settings-nav-drift")).to_have_text("1")


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
