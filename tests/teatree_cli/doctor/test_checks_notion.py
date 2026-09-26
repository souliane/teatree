"""The Notion credential probe behind ``t3 doctor`` and ``t3 setup`` — one verdict per fault.

Unconfigured, absent, rejected, shared onto nothing and a tracked page left ungranted all
end at the same dead read, and each is fixed a different way; a single "Notion is broken"
would send the operator to the wrong one. The probe runs against the real client and the
HTTP double, so what is asserted is the state the wire actually produces.
"""

import io
from contextlib import redirect_stdout
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from django.test import TestCase

from teatree.backends.types import Service
from teatree.cli.doctor.checks_notion import (
    NotionCredentialState,
    _check_notion_credentials,
    notion_routed_overlays,
    probe_notion_credential,
    report_notion_connections,
)
from teatree.config.credential_pass_key import PassKeyResolution, PassKeySource
from tests.factories import TicketFactory
from tests.teatree_backends.notion._fake_notion import FakeNotion, install_fake_notion

_MODULE = "teatree.cli.doctor.checks_notion"
_PASS_KEY = "acme/notion"
_UNSHARED_PAGE = "22222222-2222-2222-2222-222222222222"
_OTHER_OVERLAYS_PAGE = "33333333-3333-3333-3333-333333333333"
_UNSHARED_DATABASE = "44444444-4444-4444-4444-444444444444"
_UNSHARED_ALLOWED_ROOT = "55555555-5555-5555-5555-555555555555"
_UNSHARED_DENIED_ROOT = "66666666-6666-6666-6666-666666666666"


class StubOverlayConfig:
    def __init__(self, pass_key: str, *, needs_notion: bool = False, notion_database_id: str = "") -> None:
        self._pass_key = pass_key
        self.required_third_party_services = frozenset({Service.NOTION}) if needs_notion else frozenset()
        self.notion_database_id = notion_database_id

    def resolve_pass_key(self, name: str) -> PassKeyResolution:
        if name == "notion_token" and self._pass_key:
            return PassKeyResolution(f"{name}_pass_key", self._pass_key, PassKeySource.DECLARED_DEFAULT)
        return PassKeyResolution(f"{name}_pass_key", "", PassKeySource.UNSET)

    def secret_pass_key(self, name: str) -> str:
        return self.resolve_pass_key(name).value


class StubOverlay:
    def __init__(self, pass_key: str, *, needs_notion: bool = False) -> None:
        self.config = StubOverlayConfig(pass_key, needs_notion=needs_notion)


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


def _probe(pass_key: str = _PASS_KEY, *, needs_notion: bool = True, notion_database_id: str = "") -> Any:
    return probe_notion_credential(
        "acme", StubOverlayConfig(pass_key, needs_notion=needs_notion, notion_database_id=notion_database_id)
    )


def _run() -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = _check_notion_credentials()
    return ok, buf.getvalue()


class TestScope:
    def test_overlays_that_need_notion_or_route_a_key_are_probed(self) -> None:
        overlays = {
            "routed": StubOverlay(_PASS_KEY),
            "needs-it": StubOverlay("", needs_notion=True),
            "unrouted": StubOverlay(""),
        }

        with patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays):
            assert [name for name, _config in notion_routed_overlays()] == ["needs-it", "routed"]

    def test_a_box_routing_no_notion_token_passes_silently(self) -> None:
        with patch(f"{_MODULE}.notion_routed_overlays", return_value=[]):
            ok, output = _run()

        assert ok
        assert output == ""


class TestEachStateNamesItsOwnRemedy:
    def test_an_overlay_needing_notion_with_no_routed_entry_names_the_setting(self, notion: FakeNotion) -> None:
        with patch.dict("os.environ", {}, clear=False) as env:
            env.pop("NOTION_TOKEN", None)
            credential = _probe("")

        assert credential.state is NotionCredentialState.UNCONFIGURED
        assert "config_setting set notion_token_pass_key" in credential.line()
        assert notion.requests == [], "nothing is asked of Notion before the entry is even named"

    def test_no_token_names_the_setup_command(self, notion: FakeNotion, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        monkeypatch.setattr("teatree.llm.credentials.read_pass", lambda _key: "")
        monkeypatch.setattr("teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _name=None: _PASS_KEY)

        credential = _probe()

        assert credential.state is NotionCredentialState.ABSENT
        assert "t3 notion setup --overlay acme" in credential.line()
        assert "--reset" not in credential.line(), "there is nothing stored to reset"

    def test_a_rejected_token_names_the_reset_flag(self, notion: FakeNotion, stored_token: None) -> None:
        notion.identity_fail_with = (401, "unauthorized")

        credential = _probe()

        assert credential.state is NotionCredentialState.REJECTED
        assert "t3 notion setup --overlay acme --reset" in credential.line()

    def test_a_valid_token_shared_onto_nothing_names_the_sharing_step(
        self, notion: FakeNotion, stored_token: None
    ) -> None:
        notion.shared_objects = []

        credential = _probe()

        assert credential.state is NotionCredentialState.SHARED_ONTO_NOTHING
        assert "Connections" in credential.line()
        assert "bot-1" in credential.line(), "the identity pages must be shared with is the point"
        assert "t3 notion doctor" in credential.line(), "the re-check has to be the read-only command"


class TestAWorkingTokenIsCheckedAgainstTrackedPages(TestCase):
    def setUp(self) -> None:
        self.monkeypatch = pytest.MonkeyPatch()
        self.addCleanup(self.monkeypatch.undo)
        self.notion = install_fake_notion(self.monkeypatch)
        self.monkeypatch.delenv("NOTION_TOKEN", raising=False)
        self.monkeypatch.setattr(
            "teatree.llm.credentials.read_pass", lambda key: "ntn_stored" if key == _PASS_KEY else ""
        )
        self.monkeypatch.setattr(
            "teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _name=None: _PASS_KEY
        )

    def test_every_ungranted_page_an_in_flight_ticket_tracks_is_named(self) -> None:
        ticket = TicketFactory(overlay="acme", extra={"notion_url": f"https://www.notion.so/Spec-{_UNSHARED_PAGE}"})
        TicketFactory(overlay="acme", extra={"notion_url": f"https://www.notion.so/{self.notion.page_id}"})
        TicketFactory(overlay="elsewhere", extra={"notion_url": f"https://www.notion.so/{_OTHER_OVERLAYS_PAGE}"})
        self.notion.unshared_pages = {_UNSHARED_PAGE, _OTHER_OVERLAYS_PAGE}

        credential = _probe()

        assert credential.state is NotionCredentialState.UNGRANTED
        assert credential.ungranted == (f"page {_UNSHARED_PAGE} (ticket {ticket.pk})",)
        assert "Connections" in credential.line()

    def test_a_configured_database_missing_from_the_integration_grants_is_named(self) -> None:
        self.notion.unshared_databases.add(_UNSHARED_DATABASE)
        credential = _probe(notion_database_id=_UNSHARED_DATABASE)

        assert credential.state is NotionCredentialState.UNGRANTED
        assert credential.ungranted == (f"database {_UNSHARED_DATABASE} (notion_database_id)",)

    def test_only_allowed_write_roots_missing_from_the_integration_grants_are_named(self) -> None:
        self.notion.unshared_blocks.add(_UNSHARED_ALLOWED_ROOT)
        self.notion.unshared_databases.add(_UNSHARED_ALLOWED_ROOT)
        with patch(
            f"{_MODULE}.notion_write_roots",
            return_value=([_UNSHARED_ALLOWED_ROOT], [_UNSHARED_DENIED_ROOT]),
        ):
            credential = _probe()

        assert credential.state is NotionCredentialState.UNGRANTED
        assert credential.ungranted == (f"page/database {_UNSHARED_ALLOWED_ROOT} (notion_write_allowed_roots)",)
        assert "teamspace" in credential.line()

    def test_shared_database_write_root_is_not_rejected_when_block_get_returns_404(self) -> None:
        self.notion.unshared_blocks.add(_UNSHARED_ALLOWED_ROOT)

        with patch(f"{_MODULE}.notion_write_roots", return_value=([_UNSHARED_ALLOWED_ROOT], [])):
            credential = _probe()

        assert credential.state is NotionCredentialState.OK
        assert ("GET", f"/blocks/{_UNSHARED_ALLOWED_ROOT}") in self.notion.requests
        assert ("GET", f"/databases/{_UNSHARED_ALLOWED_ROOT}") in self.notion.requests

    def test_a_ticket_tracking_no_page_leaves_the_credential_ok(self) -> None:
        TicketFactory(overlay="acme", extra={})

        assert _probe().state is NotionCredentialState.OK

    def test_a_working_credential_is_ok_and_says_where_its_entry_came_from(self) -> None:
        credential = _probe()

        assert credential.state is NotionCredentialState.OK
        assert "bot-1" in credential.line()
        assert f"pass {_PASS_KEY}" in credential.line()
        assert str(PassKeySource.DECLARED_DEFAULT) in credential.line()

    def test_setup_reports_ok_lines_headless_without_opening_a_browser(self) -> None:
        routed = [("acme", StubOverlayConfig(_PASS_KEY, needs_notion=True))]
        lines: list[str] = []
        with (
            patch(f"{_MODULE}.notion_routed_overlays", return_value=routed),
            patch("webbrowser.open", side_effect=AssertionError("a headless venue has no browser")),
        ):
            ok = report_notion_connections(lines.append)

        assert ok
        assert len(lines) == 1
        assert lines[0].startswith("INFO  Notion [acme]: ")
        assert "t3 doctor check" in lines[0]

    def test_setup_notions_probe_is_two_requests_even_with_many_tickets_and_search_pages(self) -> None:
        for index in range(25):
            TicketFactory(overlay="acme", extra={"notion_url": f"https://www.notion.so/{index:032x}"})
        self.notion.search_has_more = True
        routed = [("acme", StubOverlayConfig(_PASS_KEY, needs_notion=True, notion_database_id=_UNSHARED_DATABASE))]

        with patch(f"{_MODULE}.notion_routed_overlays", return_value=routed):
            lines: list[str] = []
            assert report_notion_connections(lines.append)

        assert self.notion.requests == [("GET", "/users/me"), ("POST", "/search")]
        assert "grants not checked" in lines[0]

    def test_doctor_checks_configured_grants_directly_without_search_pagination(self) -> None:
        self.notion.search_has_more = True
        self.notion.unshared_databases.add(_UNSHARED_DATABASE)
        self.notion.unshared_blocks.add(_UNSHARED_ALLOWED_ROOT)
        self.notion.unshared_databases.add(_UNSHARED_ALLOWED_ROOT)

        with patch(f"{_MODULE}.notion_write_roots", return_value=([_UNSHARED_ALLOWED_ROOT], [])):
            credential = _probe(notion_database_id=_UNSHARED_DATABASE)

        assert credential.state is NotionCredentialState.UNGRANTED
        assert credential.ungranted == (
            f"database {_UNSHARED_DATABASE} (notion_database_id)",
            f"page/database {_UNSHARED_ALLOWED_ROOT} (notion_write_allowed_roots)",
        )
        assert self.notion.requests.count(("POST", "/search")) == 1
        assert ("GET", f"/databases/{_UNSHARED_DATABASE}") in self.notion.requests
        assert ("GET", f"/blocks/{_UNSHARED_ALLOWED_ROOT}") in self.notion.requests


class TestFailLoudNeverSkipAsPass:
    def test_a_probe_that_failed_is_reported_as_a_fault_not_as_absent(
        self, notion: FakeNotion, stored_token: None
    ) -> None:
        notion.fail_with = (503, "service_unavailable")

        credential = _probe()

        assert credential.state is NotionCredentialState.UNREACHABLE
        assert not credential.ok

    def test_a_two_hundred_carrying_no_json_is_a_fault_not_a_crash(
        self, stored_token: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _serve_html_two_hundred(monkeypatch)

        credential = _probe()

        assert credential.state is NotionCredentialState.UNREACHABLE
        assert not credential.ok

    def test_a_faulty_credential_reddens_the_exit_code_and_prints_one_line(
        self, notion: FakeNotion, stored_token: None
    ) -> None:
        notion.shared_objects = []
        routed = [("acme", StubOverlayConfig(_PASS_KEY, needs_notion=True))]

        with patch(f"{_MODULE}.notion_routed_overlays", return_value=routed):
            ok, output = _run()

        assert not ok, "a declared-but-broken Notion credential must never read as healthy"
        assert output.count("FAIL  Notion [acme]") == 1


class TestSetupReportsWhatDoctorGates:
    def test_setup_prints_the_same_line_doctor_prints_and_the_ok_lines_doctor_omits(
        self, notion: FakeNotion, stored_token: None
    ) -> None:
        notion.shared_objects = []
        routed = [
            ("acme", StubOverlayConfig(_PASS_KEY, needs_notion=True)),
            ("bare", StubOverlayConfig("", needs_notion=True)),
        ]
        with patch(f"{_MODULE}.notion_routed_overlays", return_value=routed):
            _ok, doctor_output = _run()
            lines: list[str] = []
            report_notion_connections(lines.append)

        assert [line for line in lines if line.startswith("FAIL")] == doctor_output.splitlines()
