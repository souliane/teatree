"""The import/export page in a browser — reachable on its own, and stating its scope.

The browser-level counterpart to ``tests/teatree_dash/views/test_interchange.py``: it proves
what the test client cannot — that the page is one nav click away rather than buried in the
settings page, and that the operator meets the breadth (loops, presets, schedules) before
the controls that act on it.
"""

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from e2e.dash.pom import InterchangePage, TermScan
from teatree.core.config_interchange.scope import EXPORT_SECTIONS
from teatree.hooks.banned_term_registry import load_registry
from tests.placeholder_terms import TEST_TERM_REGISTRY, TEST_TERM_REGISTRY_JSON

_SCAN = TermScan(tuple(term for terms in TEST_TERM_REGISTRY.values() for term in terms))
_CARRIER = f"{TEST_TERM_REGISTRY['leak'][0]} desk"


@pytest.fixture
def placeholder_terms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEATREE_TERM_REGISTRY", TEST_TERM_REGISTRY_JSON)


@pytest.fixture
def no_terms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEATREE_TERM_REGISTRY", raising=False)
    if load_registry() is not None:
        pytest.fail("precondition: a term registry is still configured")


@pytest.fixture
def interchange(live_server: LiveServer, page: Page) -> InterchangePage:
    screen = InterchangePage(page, live_server.url)
    screen.open()
    return screen


def _upload(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.usefixtures("seeded_board")
def test_the_page_is_its_own_nav_entry(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/settings/")
    # Scoped to the nav landmark: the settings page ALSO links here from its pointer band,
    # which is the point — reachable from both, hosted by neither but this page.
    nav = page.get_by_role("navigation", name="Dashboard sections")
    nav.get_by_role("link", name="Import / export", exact=True).click()
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


@pytest.mark.usefixtures("seeded_board", "placeholder_terms")
def test_an_import_preview_names_a_term_row_by_key_and_class(interchange: InterchangePage, tmp_path: Path) -> None:
    interchange.preview(_upload(tmp_path, f'[teatree]\nissue_implementer_label = "{_CARRIER}"\n'))
    expect(interchange.banner("1 row(s) rejected — the whole import was refused, nothing was written.")).to_be_visible()
    rejected = interchange.page.get_by_text("issue_implementer_label [global] — secret (banned-term)", exact=True)
    expect(rejected).to_be_visible()
    _SCAN.raise_if_carried(interchange.page.content())


@pytest.mark.usefixtures("seeded_board", "no_terms")
def test_with_no_term_registry_the_export_is_refused_on_the_page(interchange: InterchangePage) -> None:
    interchange.export()
    refusal = interchange.banner("Export refused:")
    expect(refusal).to_be_visible()
    expect(refusal).to_contain_text("banned_term_registry")
    interchange.raise_if_downloaded()


@pytest.mark.usefixtures("seeded_board", "no_terms")
def test_with_no_term_registry_an_import_preview_shows_every_value_withheld(
    interchange: InterchangePage, tmp_path: Path
) -> None:
    interchange.preview(_upload(tmp_path, "[teatree]\nmerge_wip = 4\n"))
    notice = interchange.banner("not content-scanned")
    expect(notice).to_be_visible()
    expect(notice).to_contain_text("banned_term_registry")
    expect(interchange.page.get_by_text("merge_wip = <withheld: private> [global]", exact=True)).to_be_visible()
    expect(interchange.page.get_by_text("rejected")).to_have_count(0)
