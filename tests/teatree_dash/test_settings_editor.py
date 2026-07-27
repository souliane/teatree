"""The model-driven settings-editor surface — masking, restore state, export/preview (D7)."""

from unittest.mock import patch

from django.test import TestCase

from teatree.config.cold_defaults import shipped_defaults_table
from teatree.config.schema import TeatreeSettingsSchema
from teatree.config.setting_groups import UNGROUPED_LABEL, group_labels
from teatree.core.config_display import MASKED, render_value
from teatree.core.models import ConfigSetting
from teatree.dash.settings_editor import (
    EditableSetting,
    build_setting_row,
    build_settings_editor,
    export_text,
    group_rows,
    import_preview,
)


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
        # drift class the shared is_secret taxonomy closed (cluster 9).
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


def _stub_row(name: str) -> EditableSetting:
    return EditableSetting(
        name=name,
        category="default",
        value="x",
        is_secret=False,
        is_safety_posture=False,
        is_overridden=False,
        shipped_default="",
        has_shipped_default=False,
        matches_shipped_default=True,
        default_comparison="",
    )


class TestGroupingDropsNothing:
    """The grouping partitions the rows — the guard against the 130-of-184 drop returning."""

    def test_the_groups_partition_the_flat_row_set_exactly(self) -> None:
        # The undeclared row is deliberate: a grouping that skips what it cannot classify
        # loses it here, which is exactly the drop this partition assertion exists to catch.
        rows = (
            _stub_row("mode"),
            _stub_row("banned_terms"),
            _stub_row("overlays"),
            _stub_row("a_key_no_declaration_base_carries"),
        )
        grouped = [s.name for group in group_rows(rows) for s in group.settings]
        assert sorted(grouped) == sorted(r.name for r in rows)
        assert len(grouped) == len(set(grouped)), "a row landed in two groups"

    def test_a_row_no_group_declares_lands_in_the_leftovers_bucket_not_nowhere(self) -> None:
        groups = group_rows((_stub_row("mode"), _stub_row("a_key_no_declaration_base_carries")))
        leftovers = next(g for g in groups if g.is_ungrouped)
        assert leftovers.label == UNGROUPED_LABEL
        assert [s.name for s in leftovers.settings] == ["a_key_no_declaration_base_carries"]

    def test_an_empty_group_is_omitted_so_the_page_has_no_hollow_sections(self) -> None:
        groups = group_rows((_stub_row("mode"),))
        assert [g.label for g in groups] == ["Mode, harness & agent runtime"]

    def test_groups_render_in_the_declared_order(self) -> None:
        rows = (_stub_row("overlays"), _stub_row("mode"), _stub_row("autoload"))
        rendered = [g.label for g in group_rows(rows)]
        assert rendered == [label for label in group_labels() if label in rendered]


class TestBuildSettingsEditorGroups(TestCase):
    def test_the_page_groups_carry_every_schema_key(self) -> None:
        view = build_settings_editor()
        grouped = {s.name for group in view.groups for s in group.settings}
        assert grouped == set(TeatreeSettingsSchema.model_fields)
        assert grouped == {s.name for s in view.settings}

    def test_no_group_is_empty(self) -> None:
        assert all(group.settings for group in build_settings_editor().groups)

    def test_a_read_failure_leaves_no_groups_to_render(self) -> None:
        with patch(
            "teatree.dash.settings_editor.ConfigSetting.objects.overrides_for_scope",
            side_effect=RuntimeError("db down"),
        ):
            view = build_settings_editor()
        assert view.groups == ()


class TestBuildSettingRow(TestCase):
    """The single-row rebuild the htmx swap answers with — same policy as the whole page."""

    def test_it_matches_the_row_the_full_page_builds(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        page_row = next(s for s in build_settings_editor().settings if s.name == "mode")
        assert build_setting_row("mode") == page_row

    def test_a_secret_row_is_masked(self) -> None:
        ConfigSetting.objects.set_value("banned_terms", ["supersecretcodename"])
        row = build_setting_row("banned_terms")
        assert row.is_secret is True
        assert row.value == MASKED

    def test_it_reads_the_requested_scope(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto", scope="proj")
        assert build_setting_row("mode", "proj").is_overridden is True
        assert build_setting_row("mode").is_overridden is False


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


class TestShippedDefaultComparison(TestCase):
    """Each row shows the shipped default and whether the effective value matches it."""

    def _row(self, key: str, scope: str = ""):
        return next(s for s in build_settings_editor(scope).settings if s.name == key)

    def test_a_key_with_a_toml_default_carries_it_on_the_row(self) -> None:
        row = self._row("mode")
        assert row.has_shipped_default is True
        assert row.shipped_default == render_value(shipped_defaults_table()["mode"])

    def test_an_unset_key_reads_as_matching_its_shipped_default(self) -> None:
        row = self._row("mode")
        assert row.is_overridden is False
        assert row.matches_shipped_default is True
        assert row.default_comparison == "same as default"

    def test_an_override_equal_to_the_shipped_default_still_reads_as_matching(self) -> None:
        ConfigSetting.objects.set_value("mode", shipped_defaults_table()["mode"])
        row = self._row("mode")
        assert row.is_overridden is True
        assert row.matches_shipped_default is True

    def test_an_override_away_from_the_shipped_default_reads_as_differing(self) -> None:
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 42)
        row = self._row("provision_ram_ceiling_percent")
        assert row.matches_shipped_default is False
        assert row.default_comparison == "differs from default"

    def test_a_key_with_no_toml_default_offers_no_comparison(self) -> None:
        # Personal/Secret keys are absent from the shipped file by construction, so there is
        # nothing to compare against — the row shows no default and no verdict.
        row = self._row("workspace_dir")
        assert row.is_secret is False
        assert row.has_shipped_default is False
        assert row.shipped_default == ""
        assert row.default_comparison == ""

    def test_a_secret_key_offers_no_comparison_and_still_masks(self) -> None:
        row = self._row("slack_user_id")
        assert row.has_shipped_default is False
        assert row.shipped_default == MASKED
        assert row.default_comparison == ""

    def test_a_secret_default_is_masked_before_it_reaches_the_row(self) -> None:
        # Belt and braces: a secret key that ever gained a shipped default still masks.
        with patch("teatree.dash.settings_editor.shipped_defaults_table", return_value={"banned_terms": ["leaky"]}):
            row = self._row("banned_terms")
        assert row.shipped_default == MASKED
        assert "leaky" not in row.shipped_default

    def test_the_comparison_text_stands_alone_without_the_colour(self) -> None:
        # Colour is not the only signal: every comparison carries words of its own.
        assert self._row("mode").default_comparison
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 42)
        assert self._row("provision_ram_ceiling_percent").default_comparison
