"""The health page: verdict lead, the three bands, and the fail-open banner.

The red banner shows whenever the master danger gate is on, on every load (#3162).
"""

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from teatree.core.models.config_setting import ConfigSetting


@pytest.mark.usefixtures("seeded_board")
def test_verdict_lead_and_the_three_bands_render(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/health/")
    expect(page.locator(".verdict")).to_be_visible()
    expect(page.get_by_role("heading", name="Loops")).to_be_visible()
    expect(page.get_by_role("heading", name="Capacity")).to_be_visible()
    expect(page.get_by_role("heading", name="Mode")).to_be_visible()


@pytest.mark.usefixtures("seeded_board")
def test_fail_open_banner_persists_across_reloads(live_server: LiveServer, page: Page) -> None:
    ConfigSetting.objects.set_value("danger_gate_fail_open", value=True)
    banner = page.locator(".banner-red").filter(has_text="danger_gate_fail_open is ON")

    page.goto(f"{live_server.url}/dash/health/")
    expect(banner).to_be_visible()
    # A fail-open factory is never healthy — the banner must not vanish on a reload.
    page.reload()
    expect(banner).to_be_visible()
