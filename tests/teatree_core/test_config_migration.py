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

from django.test import TestCase

from teatree.core.config_migration import import_toml_into_db
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
