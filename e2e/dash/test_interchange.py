"""The import/export page in a browser — reachable on its own, and stating its scope.

The browser-level counterpart to ``tests/teatree_dash/views/test_interchange.py``: it proves
what the test client cannot — that the page is one nav click away rather than buried in the
settings page, and that the operator meets the breadth (loops, presets, schedules) before
the controls that act on it.
"""

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from teatree.core.config_interchange.scope import EXPORT_SECTIONS


@pytest.mark.usefixtures("seeded_board")
def test_the_page_is_its_own_nav_entry(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/settings/")
    page.get_by_role("link", name="Import / export", exact=True).click()
    expect(page).to_have_url(f"{live_server.url}/dash/import-export/")


@pytest.mark.usefixtures("seeded_board")
def test_the_page_names_every_section_a_dump_carries(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/import-export/")
    for section in EXPORT_SECTIONS:
        expect(page.locator("body")).to_contain_text(f"[{section.table}]")


@pytest.mark.usefixtures("seeded_board")
def test_the_import_control_takes_a_file_not_a_textarea(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/import-export/")
    expect(page.get_by_label("TOML file to import")).to_be_visible()
    expect(page.locator("textarea")).to_have_count(0)


@pytest.mark.usefixtures("seeded_board")
def test_the_export_control_offers_both_filters_unticked(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/import-export/")
    for label in ("Export default keys only", "Include values that are the same as default"):
        checkbox = page.get_by_label(label)
        expect(checkbox).to_be_visible()
        expect(checkbox).not_to_be_checked()
