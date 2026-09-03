"""``_check_notion_credentials`` — the Notion credential gate and its three verdicts.

The gate exists because absent, rejected and never-shared all end at the same dead
read, and each is fixed a different way; a single "Notion is broken" would send the
operator to the wrong one. The probe runs against the real client and the HTTP double,
so what is asserted is the state the wire actually produces.
"""

import io
from contextlib import redirect_stdout
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from teatree.cli.doctor.checks_notion import (
    NotionCredentialState,
    _check_notion_credentials,
    notion_routed_overlays,
    probe_notion_credential,
)
from tests.teatree_backends.notion._fake_notion import FakeNotion, install_fake_notion

_MODULE = "teatree.cli.doctor.checks_notion"
_PASS_KEY = "acme/notion"


class StubOverlayConfig:
    def __init__(self, pass_key: str) -> None:
        self._pass_key = pass_key

    def secret_pass_key(self, name: str) -> str:
        return self._pass_key if name == "notion_token" else ""


class StubOverlay:
    def __init__(self, pass_key: str) -> None:
        self.config = StubOverlayConfig(pass_key)


@pytest.fixture
def notion(monkeypatch: pytest.MonkeyPatch) -> FakeNotion:
    return install_fake_notion(monkeypatch)


@pytest.fixture
def stored_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.setattr("teatree.llm.credentials.read_pass", lambda key: "ntn_stored" if key == _PASS_KEY else "")
    monkeypatch.setattr("teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _name=None: _PASS_KEY)


def _serve_html_two_hundred(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer every request 200 with an HTML body — a captive portal, not Notion."""
    original = httpx.Client.__init__

    def patched(self: httpx.Client, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(lambda _request: httpx.Response(200, text="<html>Sign in</html>"))
        original(self, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched)


def _run() -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = _check_notion_credentials()
    return ok, buf.getvalue()


class TestScope:
    def test_only_overlays_declaring_a_notion_pass_key_are_probed(self) -> None:
        overlays = {"acme": StubOverlay(_PASS_KEY), "unrouted": StubOverlay("")}

        with patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays):
            assert notion_routed_overlays() == [("acme", _PASS_KEY)]

    def test_a_box_routing_no_notion_token_passes_silently(self) -> None:
        with patch(f"{_MODULE}.notion_routed_overlays", return_value=[]):
            ok, output = _run()

        assert ok
        assert output == ""


class TestTheThreeStatesHaveThreeRemedies:
    def test_no_token_names_the_setup_command(self, notion: FakeNotion, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        monkeypatch.setattr("teatree.llm.credentials.read_pass", lambda _key: "")
        monkeypatch.setattr("teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _name=None: _PASS_KEY)

        credential = probe_notion_credential("acme", _PASS_KEY)

        assert credential.state is NotionCredentialState.ABSENT
        assert "t3 notion setup --overlay acme" in credential.line()
        assert "--reset" not in credential.line(), "there is nothing stored to reset"

    def test_a_rejected_token_names_the_reset_flag(self, notion: FakeNotion, stored_token: None) -> None:
        notion.identity_fail_with = (401, "unauthorized")

        credential = probe_notion_credential("acme", _PASS_KEY)

        assert credential.state is NotionCredentialState.REJECTED
        assert "t3 notion setup --overlay acme --reset" in credential.line()

    def test_a_valid_token_shared_onto_nothing_names_the_sharing_step(
        self, notion: FakeNotion, stored_token: None
    ) -> None:
        # The third state is the one every other surface reports as a 404 on one page.
        notion.shared_objects = []

        credential = probe_notion_credential("acme", _PASS_KEY)

        assert credential.state is NotionCredentialState.SHARED_ONTO_NOTHING
        assert "Connections" in credential.line()
        assert "bot-1" in credential.line(), "the identity pages must be shared with is the point"
        assert "t3 notion doctor" in credential.line(), "the re-check has to be the read-only command"
        assert "t3 notion setup" not in credential.line(), (
            "setup has no check-only mode: it always prompts for a secret, so it cannot re-CHECK a share grant"
        )

    def test_a_working_credential_is_ok(self, notion: FakeNotion, stored_token: None) -> None:
        credential = probe_notion_credential("acme", _PASS_KEY)

        assert credential.state is NotionCredentialState.OK
        assert credential.ok


class TestFailLoudNeverSkipAsPass:
    def test_a_probe_that_failed_is_reported_as_a_fault_not_as_absent(
        self, notion: FakeNotion, stored_token: None
    ) -> None:
        notion.fail_with = (503, "service_unavailable")

        credential = probe_notion_credential("acme", _PASS_KEY)

        assert credential.state is NotionCredentialState.UNREACHABLE
        assert not credential.ok

    def test_a_two_hundred_carrying_no_json_is_a_fault_not_a_crash(
        self, stored_token: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `response.json()` raises a ValueError no HTTP handler catches — taking EVERY other finding with it.
        _serve_html_two_hundred(monkeypatch)

        credential = probe_notion_credential("acme", _PASS_KEY)

        assert credential.state is NotionCredentialState.UNREACHABLE
        assert not credential.ok

    def test_a_faulty_credential_reddens_the_exit_code_and_prints_one_line(
        self, notion: FakeNotion, stored_token: None
    ) -> None:
        notion.shared_objects = []

        with patch(f"{_MODULE}.notion_routed_overlays", return_value=[("acme", _PASS_KEY)]):
            ok, output = _run()

        assert not ok, "a declared-but-broken Notion credential must never read as healthy"
        assert output.count("FAIL  Notion [acme]") == 1
