"""The always-visible loopback terminal button (top-level gap fix, #3162).

The ttyd "Debug session" button used to live only inside the per-ticket drawer, so
from the main dashboard there was no way to open a terminal. This header button
opens a fresh loopback terminal from every page; the per-ticket drawer button stays.
"""

import base64
import os
import re
import shutil
import signal
import socket
import time
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from e2e.dash.pom import BoardPage
from teatree.agents import terminal_launcher
from teatree.agents.terminal_launcher import LaunchResult

_TERMINAL = "Open a loopback terminal session"
_LOOPBACK_URL = re.compile(r"^http://127\.0\.0\.1:8804/[A-Za-z0-9_-]{22,}/$")

# The click spec drives the REAL launcher, which resolves ``shutil.which("ttyd")``
# and renders the install hint instead of a launch URL when it is absent. That is a
# HOST dependency, not a code path worth mocking away — the point of the spec is
# that the button spawns a real terminal.
#
# The dependency is DECLARED where each environment that must run this provisions
# it: ``.github/workflows/ci.yml`` apt-installs ttyd for the e2e-dash job (so the
# authoritative lane never skips and the coverage is not quietly dead), and
# ``deploy/Dockerfile`` installs it into the image the dashboard actually serves
# from. A developer host with neither gets a VISIBLE skip naming the install
# command instead of a red that reads like a product bug. Conditional by
# construction, so it is not the dead coverage ``check_no_silent_skip`` bans.
requires_ttyd = pytest.mark.skipif(
    shutil.which("ttyd") is None,
    reason="ttyd is not on PATH — install it (apt install ttyd / brew install ttyd); "
    "CI and the deploy image both ship it",
)


@pytest.mark.usefixtures("seeded_board")
def test_terminal_button_visible_on_board(live_server: LiveServer, page: Page) -> None:
    board = BoardPage(page, live_server.url)
    board.open()
    # Top-level — no drawer opened. This is the gap the button closes.
    expect(page.locator("#drawer")).to_be_empty()
    expect(page.get_by_role("button", name=_TERMINAL)).to_be_visible()


@requires_ttyd
@pytest.mark.usefixtures("seeded_board", "accept_dialogs")
def test_dashboard_launch_url_accepts_websocket(
    live_server: LiveServer, page: Page, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lsof = tmp_path / "lsof"
    lsof.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    lsof.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    page.add_init_script("window.open = function () { return null; };")
    board = BoardPage(page, live_server.url)
    board.open()
    with (
        patch("teatree.agents.web_terminal.build_claude_command", return_value=["/bin/sh"]),
        patch("teatree.agents.web_terminal.system_child_env", return_value=None),
    ):
        page.get_by_role("button", name=_TERMINAL).click()
        result = page.locator("[data-ttyd-launch]")
        expect(result).to_have_attribute("data-ttyd-launch", _LOOPBACK_URL)
        url = result.get_attribute("data-ttyd-launch")
    if url is None:
        pytest.fail("dashboard returned no terminal URL")
    if _ws_response(url, origin="http://127.0.0.1:8804") != b"HTTP/1.1 101 Switching Protocols":
        pytest.fail("dashboard terminal URL did not accept a websocket")


@requires_ttyd
@pytest.mark.usefixtures("seeded_board", "accept_dialogs")
def test_terminal_button_click_renders_launch_url(
    live_server: LiveServer, page: Page, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lsof = tmp_path / "lsof"
    lsof.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    lsof.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    board = BoardPage(page, live_server.url)
    board.open()
    with (
        patch("teatree.agents.web_terminal.build_claude_command", return_value=["/bin/sh"]),
        patch("teatree.agents.web_terminal.system_child_env", return_value=None),
        page.context.expect_page() as opened,
    ):
        page.get_by_role("button", name=_TERMINAL).click()
    result = page.locator("#terminal-result")
    # The result auto-opens a new tab; the link + data-ttyd-launch attr are the
    # popup-blocked fallback / auto-open source (directive #3).
    expect(result).to_contain_text("Opening terminal in a new tab")
    expect(result.locator("[data-ttyd-launch]")).to_have_attribute("data-ttyd-launch", _LOOPBACK_URL)
    expect(result.get_by_role("link")).to_have_attribute("href", _LOOPBACK_URL)
    expect(opened.value).to_have_url(_LOOPBACK_URL)
    opened.value.close()


@pytest.mark.usefixtures("seeded_board", "accept_dialogs")
def test_unreachable_terminal_shows_tunnel_hint_without_opening_a_tab(live_server: LiveServer, page: Page) -> None:
    board = BoardPage(page, live_server.url)
    board.open()
    page.route("http://127.0.0.1:8804/**", lambda route: route.abort())
    with patch(
        "teatree.dash.views.debug.launch_web_session",
        return_value=LaunchResult(launch_url="http://127.0.0.1:8804/unreachable/", port=8804),
    ):
        page.get_by_role("button", name=_TERMINAL).click()
        expect(page.locator("[data-ttyd-reachability]")).to_be_visible()
    if len(page.context.pages) != 1:
        pytest.fail("an unreachable terminal opened a tab")


def _ws_response(url: str, *, origin: str, path: str | None = None) -> bytes:
    parsed = urlsplit(url)
    if parsed.port is None:
        msg = "terminal URL has no port"
        raise ValueError(msg)
    key = base64.b64encode(os.urandom(16)).decode()
    request_path = path or f"{parsed.path}ws"
    request = (
        f"GET {request_path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\nOrigin: {origin}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Protocol: tty\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", parsed.port), timeout=2) as client:
        client.settimeout(2)
        client.sendall(request.encode())
        return client.recv(512).split(b"\r\n", 1)[0]


@requires_ttyd
def test_real_ttyd_websocket_requires_its_secret_path_and_origin() -> None:
    ttyd_bin = shutil.which("ttyd")
    if ttyd_bin is None:
        pytest.skip("ttyd is not on PATH")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with patch.object(
        terminal_launcher.shutil, "which", side_effect=lambda name: ttyd_bin if name == "ttyd" else "/bin/true"
    ):
        result = terminal_launcher.launch_ttyd(["/bin/sh"], port=port)
    if not result.pid:
        pytest.fail(result.error)
    if not result.launch_url:
        pytest.fail("launcher returned no URL")
    origin = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                if _ws_response(result.launch_url, origin="http://evil.example") == b"HTTP/1.1 101 Switching Protocols":
                    pytest.fail("foreign origin reached the terminal")
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        if _ws_response(result.launch_url, origin=origin, path="/ws") == b"HTTP/1.1 101 Switching Protocols":
            pytest.fail("websocket path without the token was accepted")
        if _ws_response(result.launch_url, origin=origin) != b"HTTP/1.1 101 Switching Protocols":
            pytest.fail("the terminal websocket was not accepted")
    finally:
        with suppress(ProcessLookupError):
            os.kill(result.pid, signal.SIGTERM)
