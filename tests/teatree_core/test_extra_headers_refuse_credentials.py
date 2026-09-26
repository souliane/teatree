# test-path: cross-cutting — one allowlist contract driven through the write seam, every write surface and every reader.
"""``openai_compatible_extra_headers`` carries only allowlisted headers, however it is written or read.

The map is plain config: the admin renders it and a shared export carries it. A header outside the
allowlist is refused on every write and withheld on every read, so a credential reaches the endpoint
only through ``openai_compatible_credential_entry``. Every value here is a synthetic canary.
"""

import json
from io import StringIO
from typing import Any

import django.http
import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from teatree.agents.harness import PydanticAiHarness, resolve_harness
from teatree.config import get_effective_settings
from teatree.core.config_display import MASKED
from teatree.core.config_interchange.migration import export_db_to_toml
from teatree.core.models import ConfigSetting
from teatree.core.setting_control import SettingControl
from teatree.mcp import build_server
from tests.teatree_mcp._call_tool_result import payloads

_KEY = "openai_compatible_extra_headers"
_POINTER = "openai_compatible_credential_entry"
_CANARY = "canary-not-a-real-credential"
_BENIGN = {"X-OrcaRouter-Include-Cost": "true", "X-OrcaRouter-Session-Id": "t3-{session}"}
_REFUSED_MAPS: tuple[dict[str, str], ...] = (
    {"Authorization": f"Bearer {_CANARY}"},
    {"XAuthToken": _CANARY},
    {" Authorization": _CANARY},
    {"AUTHORIZATION\t": _CANARY},
    {"x-api-key": _CANARY},
    {"Cookie": f"session={_CANARY}"},
    {**_BENIGN, "X-Upstream": _CANARY},
    {" X-OrcaRouter-Session-Id": _CANARY},
)


def _mcp_set(value: dict[str, str]) -> Any:
    return payloads(
        async_to_sync(build_server().call_tool)("config_setting_set", {"key": _KEY, "value": json.dumps(value)})
    )[0]


def _login_superuser(test: TestCase) -> None:
    test.client.force_login(get_user_model().objects.create_superuser("headers-admin", "headers@example.com", "pw"))


class TestTheWriteSeamRefusesUnlistedHeaders(TestCase):
    def test_set_value_refuses_each_unlisted_header(self) -> None:
        for headers in _REFUSED_MAPS:
            with self.subTest(headers=list(headers)), pytest.raises(ValidationError, match=_POINTER):
                ConfigSetting.objects.set_value(_KEY, headers)
        assert not ConfigSetting.objects.filter(key=_KEY).exists()

    def test_set_values_refuses_and_writes_no_row_of_the_set(self) -> None:
        for headers in _REFUSED_MAPS:
            rows = [("issue_implementer_label", "t3-auto", ""), (_KEY, headers, "")]
            with self.subTest(headers=list(headers)), pytest.raises(ValidationError, match=_POINTER):
                ConfigSetting.objects.set_values(rows)
        assert not ConfigSetting.objects.filter(key__in=[_KEY, "issue_implementer_label"]).exists()

    def test_seed_refuses_an_unlisted_header(self) -> None:
        with pytest.raises(ValidationError, match=_POINTER):
            ConfigSetting.objects.seed(_KEY, {"XAuthToken": _CANARY}, code_default={})
        assert not ConfigSetting.objects.filter(key=_KEY).exists()

    def test_allowlisted_headers_are_accepted_in_any_case(self) -> None:
        ConfigSetting.objects.set_value(_KEY, _BENIGN)
        ConfigSetting.objects.set_values([(_KEY, {"x-orcarouter-session-id": "t3-{session}"}, "")])
        assert ConfigSetting.objects.get(key=_KEY, scope="").value == {"x-orcarouter-session-id": "t3-{session}"}


class TestEveryWriteSurfaceRefusesUnlistedHeaders(TestCase):
    def setUp(self) -> None:
        _login_superuser(self)

    def _admin_change(self, row: ConfigSetting, value: dict[str, str]) -> django.http.HttpResponse:
        url = reverse("admin:core_configsetting_change", args=[row.pk])
        return self.client.post(url, {"scope": row.scope, "key": row.key, "value": json.dumps(value)})

    def test_the_cli_refuses_each_unlisted_header_without_echoing_it(self) -> None:
        for headers in _REFUSED_MAPS:
            stderr = StringIO()
            with self.subTest(headers=list(headers)), pytest.raises(SystemExit):
                call_command("config_setting", "set", _KEY, json.dumps(headers), stderr=stderr)
            assert _POINTER in stderr.getvalue()
            assert _CANARY not in stderr.getvalue()
        assert not ConfigSetting.objects.filter(key=_KEY).exists()

    def test_the_mcp_tool_refuses_each_unlisted_header(self) -> None:
        for headers in _REFUSED_MAPS:
            with self.subTest(headers=list(headers)), pytest.raises(Exception, match=_POINTER):
                _mcp_set(headers)
        assert not ConfigSetting.objects.filter(key=_KEY).exists()

    def test_the_admin_form_refuses_each_unlisted_header(self) -> None:
        row = ConfigSetting.objects.set_value(_KEY, _BENIGN)
        for headers in _REFUSED_MAPS:
            with self.subTest(headers=list(headers)):
                response = self._admin_change(row, headers)
                assert response.status_code == 200
                assert _POINTER in str(response.context["adminform"].form.errors.get("value"))
                row.refresh_from_db()
                assert row.value == _BENIGN

    def test_allowlisted_headers_are_accepted_on_every_surface(self) -> None:
        call_command("config_setting", "set", _KEY, json.dumps(_BENIGN))
        assert get_effective_settings().openai_compatible_extra_headers == _BENIGN

        routing_only = {"X-OrcaRouter-Include-Cost": "true"}
        assert _mcp_set(routing_only)["ok"] is True
        assert get_effective_settings().openai_compatible_extra_headers == routing_only

        row = ConfigSetting.objects.get(key=_KEY, scope="")
        response = self._admin_change(row, _BENIGN)
        assert response.status_code == 302
        row.refresh_from_db()
        assert row.value == _BENIGN


class TestAStoredUnlistedHeaderIsWithheldOnEveryRead(TestCase):
    def setUp(self) -> None:
        self.row = ConfigSetting.objects.create(key=_KEY, scope="", value={**_BENIGN, "XAuthToken": _CANARY})

    def test_it_is_never_sent(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        with self.assertLogs("teatree.config.extra_headers", level="WARNING") as logs:
            harness = resolve_harness(phase="coding")
        assert isinstance(harness, PydanticAiHarness)
        assert harness._backend.extra_headers == _BENIGN
        assert _CANARY not in "".join(logs.output)

    def test_it_is_never_exported(self) -> None:
        result = export_db_to_toml(scan_terms=())
        assert _CANARY not in result.toml
        assert [row.reason for row in result.redacted if row.key == _KEY] == ["unlisted-header"]

    def test_it_is_never_rendered_by_the_admin(self) -> None:
        _login_superuser(self)
        changelist = self.client.get(reverse("admin:core_configsetting_changelist")).content.decode()
        change_form = self.client.get(reverse("admin:core_configsetting_change", args=[self.row.pk])).content.decode()
        for rendered in (changelist, change_form):
            assert _CANARY not in rendered

    def test_the_settings_control_masks_it(self) -> None:
        control = SettingControl(key=_KEY)
        assert control.display_value(self.row.value) == MASKED
        assert control.wire_value(self.row.value) == MASKED
