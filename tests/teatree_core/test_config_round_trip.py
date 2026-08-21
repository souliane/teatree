# test-path: cross-cutting
"""A box can re-import its own export, and re-importing it changes nothing (#4147).

Export and import are inverses or they are neither. Two properties make that concrete on
an UNCHANGED box, and both were broken:

*   **zero rejections** — the export dumped every ``ConfigSetting`` row, including the
    internal runtime state that shares the store, and the import refuses the WHOLE file on
    one key the registry does not declare. So the file a live box wrote was the file that
    same box refused;
*   **zero writes** — a row already stored at the value the file carries was classified
    ``write``, so the preview reported a store full of changes on a box nothing had
    changed. That is what the operator saw on the dashboard.

Asserted through BOTH surfaces, because the report came from the dashboard while the
reproduction was on the CLI, and the two must not be able to disagree.
"""

import tomllib
from typing import Any, ClassVar, cast

from django.test import TestCase

from teatree.core.config_interchange.migration import export_db_to_toml, import_toml_to_db
from teatree.core.config_interchange.secret_guard import is_private_backup
from teatree.core.config_interchange.types import ConfigImport
from teatree.core.models import ConfigSetting, Loop
from teatree.dash.interchange import export_text, import_preview
from teatree.loops.preset_seed import seed_default_presets_and_schedules

#: Rows a live box holds in the ``ConfigSetting`` store that are NOT operator configuration —
#: internal state a module owns, written through the same store. The two the report named.
_INTERNAL_STATE_ROWS: dict[str, object] = {
    "host_projection_generation": 180,
    "loop_preset_transition_stamp": "maintenance",
}


class TestExportImportRoundTripIsANoOp(TestCase):
    """``export`` then ``import --dry-run`` on an unchanged box classifies nothing at all."""

    def setUp(self) -> None:
        seed_default_presets_and_schedules()
        ConfigSetting.objects.set_value("mode", "auto")
        ConfigSetting.objects.set_value("merge_wip", 4)
        ConfigSetting.objects.set_value("clean_ignore", ["c236659ccc03"])
        ConfigSetting.objects.set_value("issue_implementer_label", "scoped", scope="demo")
        for key, value in _INTERNAL_STATE_ROWS.items():
            ConfigSetting.objects.set_value(key, value)
        loop = Loop.objects.first()
        assert loop is not None, "precondition: the seed created the loops the export can diverge"
        loop.delay_seconds += 60
        loop.save(update_fields=["delay_seconds", "updated_at"])

    def _round_trip(self) -> ConfigImport:
        return import_toml_to_db(
            export_db_to_toml(scan_terms=()).toml, dry_run=True, scan_terms=(), allow_safety_posture=True
        )

    def test_the_export_is_accepted_by_the_import_that_reads_it(self) -> None:
        assert self._round_trip().rejected == ()

    def test_the_export_writes_nothing_back_into_the_store_it_came_from(self) -> None:
        result = self._round_trip()
        # A refused import writes nothing either, so the write assertion is vacuous until
        # the file is accepted at all — the rejection check is this test's precondition.
        assert result.rejected == ()
        assert result.written == ()

    def test_internal_state_rows_never_reach_the_dump(self) -> None:
        # The export's own reason the round trip holds: a row no setting declaration owns is
        # not configuration, so it is left out rather than written into a file that refuses it.
        emitted = tomllib.loads(export_db_to_toml(scan_terms=()).toml).get("teatree", {})
        assert "Ungrouped" not in emitted, f"the leftovers bucket reached the dump: {emitted}"

    def test_the_omitted_rows_are_reported_rather_than_dropped_in_silence(self) -> None:
        omitted = {row.key: row.reason for row in export_db_to_toml(scan_terms=()).omitted}
        assert set(omitted) == set(_INTERNAL_STATE_ROWS)
        assert all("internal state" in reason for reason in omitted.values()), omitted

    def test_the_operator_rows_do_survive_the_dump(self) -> None:
        # Anti-vacuous control: an export that emitted NOTHING would satisfy every assertion
        # above for free. What the round trip must carry is the operator's own config.
        dump = export_db_to_toml(scan_terms=()).toml
        assert "mode" in dump
        assert "clean_ignore" in dump

    def test_the_unchanged_rows_are_counted_as_unchanged_not_written(self) -> None:
        # `mode = "auto"` is the shipped default, so it is the older `skip` disposition; the
        # two below are genuine operator drift, which is the case that used to read as a write.
        result = self._round_trip()
        assert {row.key for row in result.unchanged} >= {"merge_wip", "clean_ignore"}


class TestARedactedExportCannotDeleteWhatItRedacted(TestCase):
    """The real box's shape: an overlays registry whose credential coordinates are withheld.

    The secret guard drops those keys from the emitted ``[overlays.<name>]`` table, so the
    file describes the overlay INCOMPLETELY. Rebuilding the registry row from that table and
    storing it wholesale would make the redaction itself the deletion — an export the guard
    made safe to share becomes a file that destroys the credential coordinates it protected.
    """

    _REGISTRY: ClassVar[dict[str, dict[str, str]]] = {
        "t3-teatree": {
            "path": "~/teatree-deploy",
            "class": "teatree.overlays.teatree.TeatreeOverlay",
            "github_token_pass_key": "teatree/github-token",
            "slack_token_ref": "teatree/slack-bot-token",
            "slack_user_id": "<the-operator>",
        }
    }

    def setUp(self) -> None:
        ConfigSetting.objects.set_value("overlays", self._REGISTRY)

    def _round_trip(self, *, dry_run: bool) -> ConfigImport:
        return import_toml_to_db(
            export_db_to_toml(scan_terms=()).toml, dry_run=dry_run, scan_terms=(), allow_safety_posture=True
        )

    def test_the_withheld_keys_are_absent_from_the_dump(self) -> None:
        # The premise the two assertions below rest on: without the withholding there is no
        # incompleteness, and the registry candidate would match the store for free.
        emitted = tomllib.loads(export_db_to_toml(scan_terms=()).toml)["overlays"]["t3-teatree"]
        assert set(emitted) == {"path", "class"}, emitted

    def test_the_incomplete_registry_table_is_not_a_write(self) -> None:
        result = self._round_trip(dry_run=True)
        assert result.rejected == ()
        assert result.written == ()

    def test_an_applied_import_leaves_the_withheld_coordinates_in_the_store(self) -> None:
        self._round_trip(dry_run=False)
        assert ConfigSetting.objects.overrides_for_scope("")["overlays"] == self._REGISTRY

    def test_an_edited_definition_still_reaches_the_store(self) -> None:
        # Anti-vacuous control: merging is what keeps a redaction from deleting, not a decision
        # to ignore the registry. A field the file DOES carry still wins, and a new overlay lands.
        import_toml_to_db(
            '[overlays.t3-teatree]\npath = "~/moved"\n[overlays.second]\npath = "~/second"\n',
            scan_terms=(),
            allow_safety_posture=True,
        )
        registry = cast("dict[str, Any]", ConfigSetting.objects.overrides_for_scope("")["overlays"])
        assert registry["t3-teatree"]["path"] == "~/moved"
        assert registry["t3-teatree"]["github_token_pass_key"] == "teatree/github-token"
        assert registry["second"] == {"path": "~/second"}


class TestAPerOverlaySettingReturnsToItsOwnRow(TestCase):
    """A per-overlay SETTING row round-trips onto the same row, never into the registry.

    The export merges an overlay's registry DEFINITIONS with its per-overlay setting rows
    into one ``[overlays.<name>]`` table. The inverse must split them back the way the export
    joined them — by whether the key is a declared setting — or a setting outside the
    ``UserSettings`` partition is relocated into the definition registry while its own row
    stays behind, leaving the value stored twice under two different meanings.
    """

    def setUp(self) -> None:
        ConfigSetting.objects.set_value("overlays", {"demo": {"path": "~/demo"}})
        ConfigSetting.objects.set_value("agent_phase_harness", {"coding": "codex"}, scope="demo")

    def _round_trip(self, *, dry_run: bool) -> ConfigImport:
        return import_toml_to_db(
            export_db_to_toml(scan_terms=()).toml, dry_run=dry_run, scan_terms=(), allow_safety_posture=True
        )

    def test_the_scoped_setting_is_emitted_under_its_overlay(self) -> None:
        emitted = tomllib.loads(export_db_to_toml(scan_terms=()).toml)["overlays"]["demo"]
        assert emitted["agent_phase_harness"] == {"coding": "codex"}

    def test_the_scoped_setting_round_trips_as_no_change_at_all(self) -> None:
        result = self._round_trip(dry_run=True)
        assert result.rejected == ()
        assert result.written == ()

    def test_an_applied_import_never_folds_the_setting_into_the_registry(self) -> None:
        self._round_trip(dry_run=False)
        assert ConfigSetting.objects.overrides_for_scope("")["overlays"] == {"demo": {"path": "~/demo"}}
        assert ConfigSetting.objects.overrides_for_scope("demo") == {"agent_phase_harness": {"coding": "codex"}}


class TestDashboardImportPreviewRoundTrip(TestCase):
    """The surface the defect was REPORTED on — the dashboard's export/preview pair."""

    def setUp(self) -> None:
        ConfigSetting.objects.set_value("autoload", value=True)
        ConfigSetting.objects.set_value("clean_ignore", ["c236659ccc03"])
        for key, value in _INTERNAL_STATE_ROWS.items():
            ConfigSetting.objects.set_value(key, value)

    def test_the_preview_of_the_pages_own_export_shows_no_change(self) -> None:
        preview = import_preview(export_text())
        assert preview.rejected == ()
        assert preview.written == ()

    def test_a_previewed_value_reads_as_the_toml_the_export_wrote(self) -> None:
        # `True` / `['c236659ccc03']` is Python repr; the file the operator uploaded says
        # `true` / `["c236659ccc03"]`, so the two rendered identical values as differences.
        ConfigSetting.objects.set_value("autoload", value=False)
        rendered = {row.key: row.toml_value for row in import_preview(export_text()).unchanged}
        assert rendered["clean_ignore"] == '["c236659ccc03"]'
        assert import_preview("[teatree]\nautoload = true\n").written[0].toml_value == "true"


class TestAPrivateBackupIsRestorable(TestCase):
    """`export --include-private` writes a file its own box can restore (#4156).

    The flag exists to make a COMPLETE backup, and the rows it adds are exactly the ones the
    import refuses by design — so the file was one nothing could read back, and nothing said
    so. The row rule is untouched: an ordinary import still refuses those rows, and a file
    that does not DECLARE itself a personal backup is refused even under the restore flag.
    What changed is that the backup says which of the two formats it is, so restoring it is a
    named act rather than an impossibility.
    """

    _TERMS: ClassVar[tuple[str, ...]] = ("acmecorp",)

    #: One row per withhold class the guard has a rule for. Every value is SYNTHETIC.
    _PRIVATE_ROWS: ClassVar[dict[str, object]] = {
        "banned_brands": ["acmebrand"],  # private-key
        "slack_user_id": "<the-operator>",  # personal-identifier
        "ban_close_trailers_on_namespaces": ["acmecorp"],  # banned-term content scan
    }

    def setUp(self) -> None:
        for key, value in self._PRIVATE_ROWS.items():
            ConfigSetting.objects.set_value(key, value)
        ConfigSetting.objects.set_value("merge_wip", 4)

    def _backup(self) -> str:
        return export_db_to_toml(include_private=True, scan_terms=self._TERMS).toml

    def _import(self, text: str, *, dry_run: bool = True, restore_private: bool = False) -> ConfigImport:
        return import_toml_to_db(
            text,
            dry_run=dry_run,
            scan_terms=self._TERMS,
            allow_safety_posture=True,
            restore_private=restore_private,
        )

    def test_a_shared_export_is_not_marked_a_personal_backup(self) -> None:
        # The premise every assertion below rests on: the marker distinguishes the two
        # formats, so it must be absent from the one an ordinary import already accepts.
        assert is_private_backup(tomllib.loads(export_db_to_toml(scan_terms=self._TERMS).toml)) is False

    def test_the_private_export_declares_itself_a_personal_backup(self) -> None:
        result = export_db_to_toml(include_private=True, scan_terms=self._TERMS)
        assert is_private_backup(tomllib.loads(result.toml)) is True
        assert result.private_backup is True

    def test_an_ordinary_import_still_refuses_it_and_says_what_it_is(self) -> None:
        result = self._import(self._backup())
        assert {row.key for row in result.rejected} == set(self._PRIVATE_ROWS)
        assert result.private_backup is True

    def test_it_restores_onto_a_store_that_lost_everything(self) -> None:
        backup = self._backup()
        ConfigSetting.objects.all().delete()
        result = self._import(backup, dry_run=False, restore_private=True)
        assert result.rejected == (), result.rejected
        rows = ConfigSetting.objects.overrides_for_scope("")
        assert {key: rows.get(key) for key in self._PRIVATE_ROWS} == self._PRIVATE_ROWS
        assert rows["merge_wip"] == 4

    def test_restoring_onto_the_box_that_wrote_it_changes_nothing(self) -> None:
        result = self._import(self._backup(), restore_private=True)
        assert result.rejected == ()
        assert result.written == ()

    def test_the_restore_flag_does_not_relax_a_file_that_is_not_a_backup(self) -> None:
        # Anti-vacuous control: the allowance is tied to the file DECLARING itself a personal
        # backup, so a shared dump carrying a secret row is refused exactly as it always was.
        result = self._import('[teatree]\nbanned_brands = ["acmebrand"]\n', restore_private=True)
        assert [(row.key, row.reason) for row in result.rejected] == [("banned_brands", "secret (private-key)")]
        assert result.private_backup is False
