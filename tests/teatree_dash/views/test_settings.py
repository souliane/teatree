"""The settings-editor POSTs write through the validating seam, mask secrets, gate safety keys (D7)."""

from django.test import TestCase
from django.urls import resolve, reverse

from teatree.core.models import ConfigSetting
from teatree.dash.views.settings import SAFETY_CONFIRM_PHRASE, settings

_LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}


class TestSettingsPage(TestCase):
    def test_page_renders_and_is_in_the_nav(self) -> None:
        response = self.client.get(reverse("dash:settings"), **_LOOPBACK)
        assert response.status_code == 200
        assert "Settings" in response.content.decode()
        # reachable from another page's nav bar
        other = self.client.get(reverse("dash:config"), **_LOOPBACK)
        assert reverse("dash:settings") in other.content.decode()

    def test_route_is_registered(self) -> None:
        assert resolve(reverse("dash:settings")).func is settings

    def test_a_configured_secret_value_never_appears_in_the_response_bytes(self) -> None:
        # THE critical guarantee: a stored secret is masked before the context is built.
        ConfigSetting.objects.set_value("banned_terms", ["supersecretcodename"])
        response = self.client.get(reverse("dash:settings"), **_LOOPBACK)
        body = response.content.decode()
        assert response.status_code == 200
        assert "supersecretcodename" not in body
        # the row is still present, masked
        assert "banned_terms" in body


class TestSettingsSet(TestCase):
    def test_set_writes_the_canonical_value_to_the_db(self) -> None:
        self.client.post(reverse("dash:settings_set"), {"key": "mode", "value": '"auto"'}, **_LOOPBACK)
        assert ConfigSetting.objects.get_effective("mode") == "auto"

    def test_set_rejects_an_unknown_key(self) -> None:
        response = self.client.post(reverse("dash:settings_set"), {"key": "not_a_setting", "value": "1"}, **_LOOPBACK)
        assert response.status_code == 400
        assert ConfigSetting.objects.count() == 0

    def test_set_rejects_an_invalid_value(self) -> None:
        response = self.client.post(
            reverse("dash:settings_set"), {"key": "issue_implementer_enabled", "value": '"false"'}, **_LOOPBACK
        )
        assert response.status_code == 400
        assert ConfigSetting.objects.count() == 0

    def test_a_secret_is_settable_but_its_value_stays_off_the_page(self) -> None:
        self.client.post(reverse("dash:settings_set"), {"key": "banned_terms", "value": '["hushword"]'}, **_LOOPBACK)
        assert ConfigSetting.objects.get_effective("banned_terms") == ["hushword"]
        body = self.client.get(reverse("dash:settings"), **_LOOPBACK).content.decode()
        assert "hushword" not in body

    def test_set_rejects_a_non_json_value(self) -> None:
        response = self.client.post(reverse("dash:settings_set"), {"key": "mode", "value": "not-json"}, **_LOOPBACK)
        assert response.status_code == 400
        assert "invalid JSON" in response.content.decode()
        assert ConfigSetting.objects.count() == 0

    def test_set_rejects_a_cross_field_inconsistent_value(self) -> None:
        # Coerces fine, but set_value's #258 consistency check refuses the pair (harness != pydantic_ai).
        response = self.client.post(
            reverse("dash:settings_set"), {"key": "agent_harness_provider", "value": '"openai_compatible"'}, **_LOOPBACK
        )
        assert response.status_code == 400
        assert "inconsistent config" in response.content.decode()
        assert ConfigSetting.objects.count() == 0

    def test_a_scoped_write_redirects_back_keeping_the_scope(self) -> None:
        response = self.client.post(
            reverse("dash:settings_set"), {"key": "mode", "value": '"auto"', "scope": "proj"}, **_LOOPBACK
        )
        assert response.status_code == 302
        assert response["Location"].endswith("?scope=proj")
        assert ConfigSetting.objects.get_effective("mode", scope="proj") == "auto"


class TestSafetyPostureConfirm(TestCase):
    def test_a_safety_posture_key_is_refused_without_the_confirm_phrase(self) -> None:
        response = self.client.post(
            reverse("dash:settings_set"), {"key": "enforce_regulated_path", "value": "true"}, **_LOOPBACK
        )
        assert response.status_code == 400
        assert ConfigSetting.objects.get_effective("enforce_regulated_path") is None

    def test_a_safety_posture_key_writes_with_the_confirm_phrase(self) -> None:
        self.client.post(
            reverse("dash:settings_set"),
            {"key": "enforce_regulated_path", "value": "true", "confirm": SAFETY_CONFIRM_PHRASE},
            **_LOOPBACK,
        )
        assert ConfigSetting.objects.get_effective("enforce_regulated_path") is True


class TestSettingsRestore(TestCase):
    def test_restore_deletes_the_db_row(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        assert ConfigSetting.objects.get_effective("mode") == "auto"
        self.client.post(reverse("dash:settings_restore"), {"key": "mode"}, **_LOOPBACK)
        assert ConfigSetting.objects.get_effective("mode") is None


class TestSettingsExport(TestCase):
    def test_export_downloads_a_dump_withholding_secrets(self) -> None:
        ConfigSetting.objects.set_value("banned_brands", ["synthetic"])
        ConfigSetting.objects.set_value("mode", "auto")
        response = self.client.get(reverse("dash:settings_export"), **_LOOPBACK)
        body = response.content.decode()
        assert response.status_code == 200
        assert response["Content-Disposition"].startswith("attachment")
        assert "synthetic" not in body
        assert "mode" in body


class TestSettingsImport(TestCase):
    def test_dry_run_preview_writes_nothing(self) -> None:
        response = self.client.post(
            reverse("dash:settings_import"), {"toml": '[teatree]\nmode = "auto"\n', "apply": ""}, **_LOOPBACK
        )
        assert response.status_code == 200
        assert "dry-run" in response.content.decode()
        assert ConfigSetting.objects.count() == 0

    def test_apply_writes_the_rows(self) -> None:
        self.client.post(
            reverse("dash:settings_import"), {"toml": '[teatree]\nmode = "auto"\n', "apply": "1"}, **_LOOPBACK
        )
        assert ConfigSetting.objects.get_effective("mode") == "auto"

    def test_apply_with_a_rejected_row_writes_nothing(self) -> None:
        response = self.client.post(
            reverse("dash:settings_import"),
            {"toml": '[teatree]\nnot_a_setting = 1\nmode = "auto"\n', "apply": "1"},
            **_LOOPBACK,
        )
        assert response.status_code == 200
        assert "rejected" in response.content.decode()
        assert ConfigSetting.objects.count() == 0
