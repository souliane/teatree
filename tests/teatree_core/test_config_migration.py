"""TOML -> ``ConfigSetting`` import service (#938 dual-read migration, TODO-75).

The reusable seam both the ``config_setting import`` management command and the
``t3 setup`` auto-migration call. Integration-first against the real DB: a raw
config dict is walked, coerced through the ``OVERLAY_OVERRIDABLE_SETTINGS``
registry, and upserted into the store — global ``[teatree]`` keys into the
global scope, ``[overlays.<name>]`` operational keys into that overlay's scope.

The new capability over the original in-command logic is the NON-CLOBBER mode:
``t3 setup`` runs on every update, so the auto-migration must never overwrite a
value the user has since changed via ``config_setting set`` — it seeds only keys
absent from the store and leaves present rows untouched.
"""

import tomllib

from django.test import TestCase

from teatree.core.config_migration import export_db_to_toml, import_toml_into_db
from teatree.core.models import ConfigSetting


class TestImportTomlIntoDb(TestCase):
    def test_seeds_global_operational_keys(self) -> None:
        raw = {"teatree": {"issue_implementer_enabled": True, "issue_implementer_max_concurrent": 4}}
        result = import_toml_into_db(raw)
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is True
        assert ConfigSetting.objects.get_effective("issue_implementer_max_concurrent") == 4
        assert result.imported == 2

    def test_skips_bootstrap_and_unknown_keys(self) -> None:
        raw = {"teatree": {"private_repos": ["acme/secret"], "not_a_real_setting": "x", "mode": "auto"}}
        result = import_toml_into_db(raw)
        assert ConfigSetting.objects.filter(key="private_repos").exists() is False
        assert ConfigSetting.objects.filter(key="not_a_real_setting").exists() is False
        assert ConfigSetting.objects.get_effective("mode") == "auto"
        assert result.imported == 1
        assert result.skipped >= 2

    def test_walks_per_overlay_table_into_overlay_scope(self) -> None:
        raw = {
            "teatree": {"mode": "interactive"},
            "overlays": {"myproj": {"path": "~/p", "mode": "auto"}},
        }
        import_toml_into_db(raw)
        assert ConfigSetting.objects.get_effective("mode") == "interactive"
        assert ConfigSetting.objects.get_effective("mode", scope="myproj") == "auto"

    def test_skips_non_setting_overlay_keys(self) -> None:
        raw = {"overlays": {"myproj": {"path": "~/p", "url": "git@x"}}}
        import_toml_into_db(raw)
        assert ConfigSetting.objects.filter(scope="myproj").exists() is False

    def test_clobber_default_overwrites_existing_row(self) -> None:
        ConfigSetting.objects.set_value("mode", "interactive")
        result = import_toml_into_db({"teatree": {"mode": "auto"}})
        assert ConfigSetting.objects.get_effective("mode") == "auto"
        assert result.overwritten == 1

    def test_clobber_is_idempotent(self) -> None:
        raw = {"teatree": {"issue_implementer_max_concurrent": 4}}
        import_toml_into_db(raw)
        import_toml_into_db(raw)
        assert ConfigSetting.objects.filter(key="issue_implementer_max_concurrent").count() == 1

    def test_no_clobber_leaves_existing_row_untouched(self) -> None:
        # A value the user set via ``config_setting set`` must survive a re-run of
        # the auto-migration: no-clobber seeds only absent keys.
        ConfigSetting.objects.set_value("mode", "auto")
        result = import_toml_into_db({"teatree": {"mode": "interactive"}}, clobber=False)
        assert ConfigSetting.objects.get_effective("mode") == "auto"
        assert result.imported == 0
        assert result.preserved == 1

    def test_no_clobber_still_seeds_absent_keys(self) -> None:
        # No-clobber is seed-if-absent, not a global skip: a key with no DB row yet
        # is still imported.
        ConfigSetting.objects.set_value("mode", "auto")
        raw = {"teatree": {"mode": "interactive", "issue_implementer_enabled": True}}
        result = import_toml_into_db(raw, clobber=False)
        assert ConfigSetting.objects.get_effective("mode") == "auto"
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is True
        assert result.imported == 1
        assert result.preserved == 1

    def test_no_clobber_per_overlay_seed_if_absent(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto", scope="myproj")
        raw = {"overlays": {"myproj": {"mode": "interactive", "issue_implementer_enabled": True}}}
        result = import_toml_into_db(raw, clobber=False)
        assert ConfigSetting.objects.get_effective("mode", scope="myproj") == "auto"
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled", scope="myproj") is True
        assert result.imported == 1
        assert result.preserved == 1

    def test_invalid_value_is_skipped_not_fatal(self) -> None:
        # A TOML value that JSON-shapes but is invalid for the setting's type is
        # skipped with a recorded reason — never an exception aborting the import.
        raw = {"teatree": {"mode": "not_a_mode", "issue_implementer_enabled": True}}
        result = import_toml_into_db(raw)
        assert ConfigSetting.objects.filter(key="mode").exists() is False
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is True
        assert result.imported == 1
        assert any("mode" in line for line in result.skipped_reasons)

    def test_result_rows_describe_each_imported_setting(self) -> None:
        raw = {"teatree": {"mode": "auto"}, "overlays": {"myproj": {"mode": "interactive"}}}
        result = import_toml_into_db(raw)
        rendered = result.summary()
        assert "global" in rendered
        assert "myproj" in rendered
        assert "2" in rendered


class TestColdHookSettingsImport(TestCase):
    """Lossless TOML->DB import of the pre-Django cold-hook settings (config-unify PR2).

    The hook-only gate flags + integer budgets the cold layer reads from
    ``~/.teatree.toml`` used to be dropped on import. The import now unions
    ``OVERLAY_OVERRIDABLE_SETTINGS`` with ``COLD_HOOK_SETTINGS`` for the global
    ``[teatree]`` table, so a non-default value survives the cutover to the DB
    store. Readers still hit TOML this PR — the import is purely additive.
    """

    def test_lossless_round_trip_for_a_spread_of_cold_hook_settings(self) -> None:
        raw = {
            "teatree": {
                "self_dm_gate_enabled": False,  # default True, flipped off
                "dispatch_quote_gate_on_task_create_enabled": True,  # default False, flipped on
                "deny_circuit_breaker_threshold": 7,  # raised threshold
                "orchestrator_turn_budget": 40,  # raised budget
                "issue_implementer_enabled": True,  # an OVERLAY_OVERRIDABLE key — union still works
            },
        }
        result = import_toml_into_db(raw)
        assert ConfigSetting.objects.get_effective("self_dm_gate_enabled") is False
        assert ConfigSetting.objects.get_effective("dispatch_quote_gate_on_task_create_enabled") is True
        assert ConfigSetting.objects.get_effective("deny_circuit_breaker_threshold") == 7
        assert ConfigSetting.objects.get_effective("orchestrator_turn_budget") == 40
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is True
        for key in ("self_dm_gate_enabled", "deny_circuit_breaker_threshold", "orchestrator_turn_budget"):
            assert ConfigSetting.objects.get_effective(key, scope="") is not None
        assert result.imported == 5

    def test_json_typed_values_land_with_correct_python_type(self) -> None:
        raw = {"teatree": {"deny_circuit_breaker_threshold": 9, "banned_terms_gate_enabled": False}}
        import_toml_into_db(raw)
        threshold = ConfigSetting.objects.get_effective("deny_circuit_breaker_threshold")
        gate = ConfigSetting.objects.get_effective("banned_terms_gate_enabled")
        assert threshold == 9
        assert isinstance(threshold, int)
        assert not isinstance(threshold, bool)
        assert gate is False

    def test_reimport_no_clobber_preserves_db_value(self) -> None:
        ConfigSetting.objects.set_value("self_dm_gate_enabled", value=False)
        result = import_toml_into_db({"teatree": {"self_dm_gate_enabled": True}}, clobber=False)
        assert ConfigSetting.objects.get_effective("self_dm_gate_enabled") is False
        assert result.preserved == 1
        assert result.imported == 0

    def test_reimport_is_idempotent(self) -> None:
        raw = {"teatree": {"orchestrator_turn_wall_clock_seconds": 240}}
        import_toml_into_db(raw)
        import_toml_into_db(raw)
        assert ConfigSetting.objects.filter(key="orchestrator_turn_wall_clock_seconds").count() == 1
        assert ConfigSetting.objects.get_effective("orchestrator_turn_wall_clock_seconds") == 240

    def test_cold_hook_keys_are_global_only_never_overlay_scoped(self) -> None:
        # The cold reader consults only the global [teatree] table for these, so an
        # [overlays.<name>] gate flag must never be mis-scoped to an overlay row.
        raw = {"overlays": {"myproj": {"deny_circuit_breaker_threshold": 9, "mode": "auto"}}}
        result = import_toml_into_db(raw)
        assert ConfigSetting.objects.filter(key="deny_circuit_breaker_threshold", scope="myproj").exists() is False
        assert ConfigSetting.objects.get_effective("mode", scope="myproj") == "auto"
        assert result.imported == 1

    def test_invalid_cold_hook_value_is_skipped_not_fatal(self) -> None:
        # A quoted "false" for a bool gate (a str, not a bool) is rejected loud and
        # skipped — never silently truthy-coerced, never aborting the rest.
        raw = {"teatree": {"self_dm_gate_enabled": "false", "banned_terms_gate_enabled": False}}
        result = import_toml_into_db(raw)
        assert ConfigSetting.objects.filter(key="self_dm_gate_enabled").exists() is False
        assert ConfigSetting.objects.get_effective("banned_terms_gate_enabled") is False
        assert any("self_dm_gate_enabled" in line for line in result.skipped_reasons)


class TestExportDbToToml(TestCase):
    """``ConfigSetting`` store -> TOML export — the precise inverse of import (PR6).

    Serialises the DB override store back to TOML so the import/export pair is a
    full round-trip interchange: global rows -> ``[teatree]``, each overlay scope
    -> ``[overlays.<name>]``, each stored value rendered as its native TOML scalar.
    """

    def test_global_rows_render_under_teatree_table(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", 3)
        doc = tomllib.loads(export_db_to_toml())
        assert doc["teatree"]["mode"] == "auto"
        assert doc["teatree"]["issue_implementer_max_concurrent"] == 3

    def test_overlay_rows_render_under_overlays_name_table(self) -> None:
        ConfigSetting.objects.set_value("mode", "interactive", scope="myproj")
        doc = tomllib.loads(export_db_to_toml())
        assert doc["overlays"]["myproj"]["mode"] == "interactive"
        # An overlay-only store carries no global [teatree] table.
        assert "teatree" not in doc

    def test_native_scalar_types_round_trip(self) -> None:
        # Each JSON-stored value decodes to its native TOML scalar, not a string.
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True)
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", 5)
        ConfigSetting.objects.set_value("issue_implementer_label", "ready")
        ConfigSetting.objects.set_value("excluded_skills", ["foo", "bar"])
        teatree = tomllib.loads(export_db_to_toml())["teatree"]
        assert teatree["issue_implementer_enabled"] is True
        assert teatree["issue_implementer_max_concurrent"] == 5
        assert isinstance(teatree["issue_implementer_max_concurrent"], int)
        assert teatree["issue_implementer_label"] == "ready"
        assert teatree["excluded_skills"] == ["foo", "bar"]

    def test_overlay_filter_dumps_only_that_overlay(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")  # global
        ConfigSetting.objects.set_value("mode", "interactive", scope="myproj")
        ConfigSetting.objects.set_value("mode", "auto", scope="other")
        doc = tomllib.loads(export_db_to_toml(overlay="myproj"))
        assert doc["overlays"]["myproj"]["mode"] == "interactive"
        assert "teatree" not in doc
        assert "other" not in doc["overlays"]

    def test_empty_store_exports_empty_document(self) -> None:
        assert export_db_to_toml().strip() == ""

    def test_export_import_export_is_a_fixed_point(self) -> None:
        # Operational + cold-hook keys across global and overlay scopes survive an
        # export -> import (into a cleared store) -> export with no drift.
        ConfigSetting.objects.set_value("mode", "auto")
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", 3)
        ConfigSetting.objects.set_value("excluded_skills", ["foo", "bar"])
        ConfigSetting.objects.set_value("orchestrator_turn_budget", 40)  # cold-hook, global-only
        ConfigSetting.objects.set_value("self_dm_gate_enabled", value=False)  # cold-hook
        ConfigSetting.objects.set_value("mode", "interactive", scope="myproj")
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True, scope="myproj")

        first = export_db_to_toml()
        # Anti-vacuity: the fixed point is meaningless unless the first export
        # actually carried the seeded scopes.
        assert "[teatree]" in first
        assert "[overlays.myproj]" in first
        ConfigSetting.objects.all().delete()
        import_toml_into_db(tomllib.loads(first))
        second = export_db_to_toml()
        assert second == first
