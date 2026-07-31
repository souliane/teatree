"""The model-driven settings-editor surface — sections, masking, provenance, export (D7).

The page renders ONE section at a time, so the never-drop guarantee moved from "every key
is on the page" to "every key is in exactly one section, and every section has a pane".
That is asserted here over the whole section list, which is the same partition the old
single-page assertion made — just held across requests instead of within one.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.config.cold_defaults import shipped_defaults_table
from teatree.config.provenance import ValueSource
from teatree.config.schema import TeatreeSettingsSchema
from teatree.config.setting_groups import UNGROUPED_PATH, setting_group_path
from teatree.core.config_display import MASKED, render_value
from teatree.core.models import ConfigSetting
from teatree.dash.settings_editor import (
    build_setting_row,
    build_settings_editor,
    build_settings_group,
    build_settings_sections,
    export_text,
    import_preview,
)


def _section_of(key: str) -> str:
    """The slug of the section *key* belongs to — the one request that renders its row."""
    path = setting_group_path(key)
    return next(section.slug for section in build_settings_sections() if section.path == path)


def _row(key: str, scope: str = ""):
    return next(s for s in build_settings_group(_section_of(key), scope).settings if s.name == key)


class TestSectionsPartitionTheSchema:
    """The guard against the 130-of-184 drop returning, now held across sections."""

    def test_every_schema_key_lands_in_exactly_one_section(self) -> None:
        rendered = [key for section in build_settings_sections() for key in _keys(section.slug)]
        assert sorted(rendered) == sorted(TeatreeSettingsSchema.model_fields)
        assert len(rendered) == len(set(rendered)), "a key landed in two sections"

    def test_no_section_is_empty(self) -> None:
        assert all(section.key_count for section in build_settings_sections())

    def test_the_nav_count_is_the_pane_it_opens(self) -> None:
        for section in build_settings_sections():
            assert section.key_count == len(build_settings_group(section.slug).settings), section.slug

    def test_each_top_level_area_lists_contiguously(self) -> None:
        # The nav is a flat list of leaves, so the hierarchy is only legible if a parent's
        # sections sit together — a scattered subtree reads as unrelated sections.
        areas = [section.path[0] for section in build_settings_sections()]
        assert _run_starts(areas) == list(dict.fromkeys(areas))

    def test_the_ungrouped_bucket_is_listed_last_when_it_has_members(self) -> None:
        declared = setting_group_path
        with patch(
            "teatree.config.setting_groups.setting_group_path",
            side_effect=lambda key: UNGROUPED_PATH if key == "mode" else declared(key),
        ):
            assert build_settings_sections()[-1].path == UNGROUPED_PATH

    def test_a_key_no_group_declares_gets_a_section_of_its_own(self) -> None:
        declared = setting_group_path
        with patch(
            "teatree.config.setting_groups.setting_group_path",
            side_effect=lambda key: UNGROUPED_PATH if key == "mode" else declared(key),
        ):
            leftovers = next(s for s in build_settings_sections() if s.path == UNGROUPED_PATH)
            assert [row.name for row in build_settings_group(leftovers.slug).settings] == ["mode"]

    def test_a_slug_naming_no_section_falls_back_rather_than_erroring(self) -> None:
        view = build_settings_group("no-such-section")
        assert view.section is not None
        assert view.settings
        assert view.error == ""


def _keys(slug: str) -> list[str]:
    return [row.name for row in build_settings_group(slug).settings]


def _run_starts(labels: list[str]) -> list[str]:
    """Each position where *labels* changes value — equal to the distinct labels iff contiguous."""
    return [label for index, label in enumerate(labels) if index == 0 or labels[index - 1] != label]


class TestMaskingAndOverrideState(TestCase):
    def test_a_stored_secret_value_is_masked_not_shown(self) -> None:
        ConfigSetting.objects.set_value("banned_terms", ["supersecretcodename"])
        row = _row("banned_terms")
        assert row.is_secret is True
        assert row.value == MASKED
        assert "supersecretcodename" not in row.value

    def test_a_secret_default_is_also_masked(self) -> None:
        # No override — the secret still renders MASKED, never its (empty) default.
        assert _row("banned_terms").value == MASKED

    def test_a_personal_identifier_not_on_the_denylist_is_masked(self) -> None:
        # slack_user_id is a personal identifier (NOT in SECRET_SETTINGS) — the exact
        # drift class the shared is_secret taxonomy closed (cluster 9).
        ConfigSetting.objects.set_value("slack_user_id", "U-OWNER-SECRET")
        row = _row("slack_user_id")
        assert row.is_secret is True
        assert row.value == MASKED
        assert "U-OWNER-SECRET" not in row.value

    def test_a_non_secret_override_shows_its_value(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        row = _row("mode")
        assert row.value == "auto"
        assert row.is_overridden is True

    def test_an_unset_key_is_not_overridden(self) -> None:
        assert _row("mode").is_overridden is False

    def test_safety_posture_keys_are_flagged(self) -> None:
        assert _row("enforce_regulated_path").is_safety_posture is True
        assert _row("mode").is_safety_posture is False


class TestValueProvenance(TestCase):
    """Each row names the TIER its effective value came from, not the setting's kind.

    The retired ``category`` column showed ``default`` for hundreds of consecutive rows —
    the setting's KIND — while sitting beside a shipped-default column, so it read as
    "this value came from the default" even on a row that said it differs from one.
    """

    def test_an_unset_key_comes_from_the_shipped_file(self) -> None:
        assert _row("mode").source == ValueSource.SHIPPED_FILE.value

    def test_a_global_row_says_so(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        assert _row("mode").source == ValueSource.DB_GLOBAL.value

    def test_an_overlay_row_beats_the_global_one_and_says_which(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        ConfigSetting.objects.set_value("mode", "interactive", scope="proj")
        assert _row("mode", "proj").source == ValueSource.DB_OVERLAY.value
        assert _row("mode").source == ValueSource.DB_GLOBAL.value

    def test_an_env_override_is_named_rather_than_hidden(self) -> None:
        with patch.dict("os.environ", {"T3_MERGE_WIP": "9"}):
            row = _row("merge_wip")
        assert row.source == ValueSource.ENV.value
        assert row.value == "9"

    def test_the_value_and_its_source_never_disagree(self) -> None:
        # One resolution, so a row cannot show a DB value while naming the shipped file.
        ConfigSetting.objects.set_value("merge_wip", 7)
        row = _row("merge_wip")
        assert (row.value, row.source, row.is_overridden) == ("7", ValueSource.DB_GLOBAL.value, True)


class TestBuildSettingRow(TestCase):
    """The single-row rebuild the htmx swap answers with — same policy as the whole pane."""

    def test_it_matches_the_row_the_pane_builds(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        assert build_setting_row("mode") == _row("mode")

    def test_a_secret_row_is_masked(self) -> None:
        ConfigSetting.objects.set_value("banned_terms", ["supersecretcodename"])
        row = build_setting_row("banned_terms")
        assert row.is_secret is True
        assert row.value == MASKED

    def test_it_reads_the_requested_scope(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto", scope="proj")
        assert build_setting_row("mode", "proj").is_overridden is True
        assert build_setting_row("mode").is_overridden is False


class TestReadFailureDegrades(TestCase):
    def test_a_pane_read_failure_degrades_to_a_visible_error(self) -> None:
        with patch("teatree.dash.settings_editor.resolve_settings", side_effect=RuntimeError("db down")):
            view = build_settings_group(_section_of("mode"))
        assert view.settings == ()
        assert "read failed" in view.error

    def test_a_page_read_failure_degrades_to_a_visible_error(self) -> None:
        with patch("teatree.dash.settings_editor.available_scopes", side_effect=RuntimeError("db down")):
            view = build_settings_editor()
        assert view.sections == ()
        assert "read failed" in view.error


class TestExportAndPreview(TestCase):
    def test_export_withholds_secret_keeps_personal(self) -> None:
        ConfigSetting.objects.set_value("banned_brands", ["synthetic"])  # secret
        ConfigSetting.objects.set_value("workspace_dir", "/tmp/ws")  # personal, non-secret
        dump = export_text()
        assert "banned_brands" not in dump
        assert "synthetic" not in dump
        assert "/tmp/ws" in dump

    def test_the_two_filters_default_to_off(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        assert export_text() == export_text(default_keys_only=False, include_defaults=False)
        assert "merge_wip" not in export_text()

    def test_both_filters_produce_the_defaults_shape(self) -> None:
        dump = export_text(default_keys_only=True, include_defaults=True)
        assert "merge_wip" in dump
        assert dump.startswith("# teatree shipped defaults")

    def test_import_preview_is_a_dry_run(self) -> None:
        result = import_preview('[teatree]\nmode = "interactive"\n')
        assert result.dry_run is True
        assert [(r.scope, r.key) for r in result.written] == [("", "mode")]
        assert ConfigSetting.objects.count() == 0


class TestShippedDefaultComparison(TestCase):
    """Each row shows the shipped default and whether the effective value matches it."""

    def test_a_key_with_a_toml_default_carries_it_on_the_row(self) -> None:
        row = _row("mode")
        assert row.has_shipped_default is True
        assert row.shipped_default == render_value(shipped_defaults_table()["mode"])

    def test_an_unset_key_reads_as_matching_its_shipped_default(self) -> None:
        row = _row("mode")
        assert row.is_overridden is False
        assert row.matches_shipped_default is True
        assert row.default_comparison == "same as default"

    def test_an_override_equal_to_the_shipped_default_still_reads_as_matching(self) -> None:
        ConfigSetting.objects.set_value("mode", shipped_defaults_table()["mode"])
        row = _row("mode")
        assert row.is_overridden is True
        assert row.matches_shipped_default is True

    def test_an_override_away_from_the_shipped_default_reads_as_differing(self) -> None:
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 42)
        row = _row("provision_ram_ceiling_percent")
        assert row.matches_shipped_default is False
        assert row.default_comparison == "differs from default"

    def test_a_key_with_no_toml_default_offers_no_comparison(self) -> None:
        # Personal/Secret keys are absent from the shipped file by construction, so there is
        # nothing to compare against — the row shows no default and no verdict.
        row = _row("workspace_dir")
        assert row.is_secret is False
        assert row.has_shipped_default is False
        assert row.shipped_default == ""
        assert row.default_comparison == ""

    def test_a_secret_key_offers_no_comparison_and_still_masks(self) -> None:
        row = _row("slack_user_id")
        assert row.has_shipped_default is False
        assert row.shipped_default == MASKED
        assert row.default_comparison == ""

    def test_a_secret_default_is_masked_before_it_reaches_the_row(self) -> None:
        # Belt and braces: a secret key that ever gained a shipped default still masks.
        with patch("teatree.dash.settings_editor.shipped_defaults_table", return_value={"banned_terms": ["leaky"]}):
            row = _row("banned_terms")
        assert row.shipped_default == MASKED
        assert "leaky" not in row.shipped_default

    def test_the_comparison_text_stands_alone_without_the_colour(self) -> None:
        # Colour is not the only signal: every comparison carries words of its own.
        assert _row("mode").default_comparison
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 42)
        assert _row("provision_ram_ceiling_percent").default_comparison
