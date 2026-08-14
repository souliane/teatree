"""The import/export page's view models — the export seam and the breadth of an import.

The page's whole reason to exist is that the file reaches past the settings store (#4340),
so what it claims an apply would touch is asserted per FAMILY, not as one row count.
"""

from django.test import TestCase

from teatree.core.models import ConfigSetting, Loop
from teatree.dash.interchange import changed_sections, export_text, import_preview

_TUNED_LOOP = "housekeeping"


class TestExport(TestCase):
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

    def test_a_tuned_loop_rides_the_same_dump(self) -> None:
        Loop.objects.filter(name=_TUNED_LOOP).update(delay_seconds=4242)
        assert f"[loops.{_TUNED_LOOP}]" in export_text()


class TestImportPreview(TestCase):
    def test_import_preview_is_a_dry_run(self) -> None:
        result = import_preview('[teatree]\nmode = "interactive"\n')
        assert result.dry_run is True
        assert [(r.scope, r.key) for r in result.written] == [("", "mode")]
        assert ConfigSetting.objects.count() == 0

    def test_a_loop_row_is_previewed_without_being_written(self) -> None:
        preview = import_preview(f"[loops.{_TUNED_LOOP}]\ndelay_seconds = 4242\n")
        assert preview.written[0].scope == f"loops.{_TUNED_LOOP}"
        assert Loop.objects.get(name=_TUNED_LOOP).delay_seconds != 4242


class TestChangedSections(TestCase):
    """The breadth an apply would touch, per top-level section rather than one total."""

    def _labels(self, text: str) -> dict[str, int]:
        return {change.section.label: change.count for change in changed_sections(import_preview(text))}

    def test_a_settings_only_file_names_the_settings_section_alone(self) -> None:
        assert self._labels('[teatree]\nmode = "interactive"\n') == {"Config settings": 1}

    def test_a_loop_row_is_counted_under_loops_not_under_settings(self) -> None:
        assert self._labels(f"[loops.{_TUNED_LOOP}]\ndelay_seconds = 4242\n") == {"Loops": 1}

    def test_a_mixed_file_names_every_section_it_touches(self) -> None:
        text = f'[teatree]\nmode = "interactive"\n\n[loops.{_TUNED_LOOP}]\ndelay_seconds = 4242\n'
        assert self._labels(text) == {"Config settings": 1, "Loops": 1}

    def test_an_overlay_scoped_row_is_counted_under_overlays(self) -> None:
        assert self._labels("[overlays.demo]\nmerge_wip = 7\n") == {"Overlays": 1}

    def test_a_file_that_changes_nothing_names_no_section(self) -> None:
        assert self._labels("") == {}

    def test_the_sections_keep_the_order_the_dump_writes_them_in(self) -> None:
        text = f'[loops.{_TUNED_LOOP}]\ndelay_seconds = 4242\n\n[teatree]\nmode = "interactive"\n'
        assert list(self._labels(text)) == ["Config settings", "Loops"]
