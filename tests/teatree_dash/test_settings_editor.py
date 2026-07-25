"""The model-driven settings-editor surface — masking, restore state, export/preview (D7)."""

from unittest.mock import patch

from django.test import TestCase

from teatree.config.schema import TeatreeSettingsSchema
from teatree.core.models import ConfigSetting
from teatree.dash.config_display import MASKED
from teatree.dash.settings_editor import build_settings_editor, export_text, import_preview


class TestBuildSettingsEditor(TestCase):
    def _row(self, key: str):
        return next(s for s in build_settings_editor().settings if s.name == key)

    def test_lists_every_schema_key(self) -> None:
        names = {s.name for s in build_settings_editor().settings}
        assert names == set(TeatreeSettingsSchema.model_fields)

    def test_a_stored_secret_value_is_masked_not_shown(self) -> None:
        ConfigSetting.objects.set_value("banned_terms", ["supersecretcodename"])
        row = self._row("banned_terms")
        assert row.is_secret is True
        assert row.value == MASKED
        assert "supersecretcodename" not in row.value

    def test_a_secret_default_is_also_masked(self) -> None:
        # No override — the secret still renders MASKED, never its (empty) default.
        assert self._row("banned_terms").value == MASKED

    def test_a_personal_identifier_not_on_the_denylist_is_masked(self) -> None:
        # slack_user_id is a personal identifier (NOT in SECRET_SETTINGS) — the exact
        # drift class: masked here, and now masked on the config surface too (cluster 9).
        ConfigSetting.objects.set_value("slack_user_id", "U-OWNER-SECRET")
        row = self._row("slack_user_id")
        assert row.is_secret is True
        assert row.value == MASKED
        assert "U-OWNER-SECRET" not in row.value

    def test_a_non_secret_override_shows_its_value(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        row = self._row("mode")
        assert row.value == "auto"
        assert row.is_overridden is True

    def test_an_unset_key_is_not_overridden(self) -> None:
        assert self._row("mode").is_overridden is False

    def test_safety_posture_keys_are_flagged(self) -> None:
        assert self._row("enforce_regulated_path").is_safety_posture is True
        assert self._row("mode").is_safety_posture is False

    def test_a_read_failure_degrades_to_a_visible_error_page(self) -> None:
        with patch(
            "teatree.dash.settings_editor.ConfigSetting.objects.overrides_for_scope",
            side_effect=RuntimeError("db down"),
        ):
            view = build_settings_editor()
        assert view.settings == ()
        assert view.error is not None
        assert "read failed" in view.error


class TestExportAndPreview(TestCase):
    def test_export_withholds_secret_keeps_personal(self) -> None:
        ConfigSetting.objects.set_value("banned_brands", ["synthetic"])  # secret
        ConfigSetting.objects.set_value("workspace_dir", "/tmp/ws")  # personal, non-secret
        dump = export_text()
        assert "banned_brands" not in dump
        assert "synthetic" not in dump
        assert "/tmp/ws" in dump

    def test_import_preview_is_a_dry_run(self) -> None:
        result = import_preview('[teatree]\nmode = "auto"\n')
        assert result.dry_run is True
        assert [(r.scope, r.key) for r in result.written] == [("", "mode")]
        assert ConfigSetting.objects.count() == 0
