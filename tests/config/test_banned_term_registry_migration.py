"""Legacy banned-term rows retain their gate classes at cutover."""

import json
from importlib import import_module
from pathlib import Path

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

_BEFORE = ("core", "0119_rename_the_architectural_review_skill")
_AFTER = ("core", "0120_merge_banned_term_registry")
_LEGACY_KEYS = ("banned_brands", "banned_terms", "banned_terms_allowlist", "overlay_leak_terms")


def _merge(existing: dict | None, legacy: dict) -> dict:
    migration = import_module("teatree.core.migrations.0120_merge_banned_term_registry")
    return migration.merge_legacy_terms(existing, legacy)


def test_legacy_rows_keep_their_gate_classes() -> None:
    result = _merge(
        None,
        {
            "banned_brands": ["brand"],
            "banned_terms": ["prose"],
            "banned_terms_allowlist": ["allowed"],
            "overlay_leak_terms": ["private"],
        },
    )
    assert result == {
        "leak": ["brand"],
        "prose_collider": ["prose"],
        "allow": ["allowed"],
        "overlay": ["private"],
        "tone": [],
    }


def test_existing_registry_and_legacy_rows_are_unioned() -> None:
    result = _merge(
        {"leak": ["new"], "prose_collider": ["current"], "tone": []},
        {"banned_brands": ["old"], "banned_terms": ["old prose"]},
    )
    assert result["leak"] == ["new", "old"]
    assert result["prose_collider"] == ["current", "old prose"]


def test_duplicate_legacy_terms_are_deduplicated_after_existing_terms() -> None:
    result = _merge({"leak": ["current"]}, {"banned_brands": ["current", "old", "old"]})
    assert result["leak"] == ["current", "old"]


@pytest.mark.parametrize("legacy", [{"banned_terms": ["prose"]}, {"banned_brands": ["brand"]}])
def test_partial_legacy_install_migrates(legacy: dict) -> None:
    result = _merge(None, legacy)
    for key, term_class in (("banned_terms", "prose_collider"), ("banned_brands", "leak")):
        if key in legacy:
            assert result[term_class] == legacy[key]


def test_a_bare_string_legacy_value_migrates_as_one_term() -> None:
    assert _merge(None, {"banned_terms": "prose"})["prose_collider"] == ["prose"]


def test_an_unreadable_legacy_value_is_skipped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    result = _merge(None, {"banned_brands": ["brand"], "banned_terms": {"not": "a list"}})
    assert result["leak"] == ["brand"]
    assert result["prose_collider"] == []
    assert "banned_terms" in caplog.text


def test_every_removed_setting_has_a_row_cutover() -> None:
    migration = import_module("teatree.core.migrations.0120_merge_banned_term_registry")
    manifest = Path(__file__).parents[2] / "src/teatree/config/setting_decisions.json"
    removed = {
        key
        for key, row in json.loads(manifest.read_text(encoding="utf-8"))["decisions"].items()
        if row["decision"] == "remove"
    }
    assert not removed
    assert set(migration.RETIRED_SETTING_KEYS).isdisjoint(json.loads(manifest.read_text())["decisions"])
    assert set(migration.LEGACY_CLASSES) == {
        "banned_brands",
        "banned_terms",
        "banned_terms_allowlist",
        "overlay_leak_terms",
    }


class TestRemovedSettingRows(TestCase):
    def test_retired_rows_are_deleted_without_touching_live_rows(self) -> None:
        migration = import_module("teatree.core.migrations.0120_merge_banned_term_registry")
        setting = apps.get_model("core", "ConfigSetting")
        scope = "settings-purge-probe"
        setting.objects.bulk_create(
            [
                setting(scope=scope, key="adaptive_intake_concurrency_enabled", value=True),
                setting(scope=scope, key="allow_destructive_disk", value=True),
                setting(scope=scope, key="autoload", value=True),
            ]
        )

        class SchemaEditor:
            connection = connection

        migration.retire_settings(apps, SchemaEditor())
        assert list(setting.objects.filter(scope=scope).values_list("key", flat=True)) == ["autoload"]


class TestShippedLoopDescriptionsRefresh(TestCase):
    def test_shipped_descriptions_are_replaced_and_operator_edits_are_kept(self) -> None:
        migration = import_module("teatree.core.migrations.0120_merge_banned_term_registry")
        loop = apps.get_model("core", "Loop")
        (outer_retired, outer_current) = migration.SHIPPED_LOOP_DESCRIPTIONS["outer_loop"]
        (heal_retired, heal_current) = migration.SHIPPED_LOOP_DESCRIPTIONS["ci_eval_heal"]
        assert heal_retired.endswith(
            "Default-OFF (autonomous CI mutation); an operator opens sessions and enables the row."
        )
        loop.objects.update_or_create(name="outer_loop", defaults={"description": outer_retired})
        loop.objects.update_or_create(name="ci_eval_heal", defaults={"description": heal_retired})
        loop.objects.update_or_create(name="directive_loop", defaults={"description": "operator wording"})

        class SchemaEditor:
            connection = connection

        migration.refresh_loop_descriptions(apps, SchemaEditor())
        descriptions = dict(loop.objects.values_list("name", "description"))
        assert descriptions["outer_loop"] == outer_current
        assert descriptions["ci_eval_heal"] == heal_current
        assert descriptions["directive_loop"] == "operator wording"


@pytest.mark.timeout(240)
class TestLegacyRowsMergeOnAnExistingDatabase(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    @staticmethod
    def _migrate(target: tuple[str, str]):
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        return executor.loader.project_state(target).apps.get_model("core", "ConfigSetting")

    def _seed_legacy_and_registry_rows(self) -> None:
        setting = self._migrate(_BEFORE)
        setting.objects.filter(key__in=[*_LEGACY_KEYS, "banned_term_registry"]).delete()
        setting.objects.bulk_create(
            [
                setting(scope="", key="banned_brands", value=["legacy-brand", "shared"]),
                setting(scope="", key="banned_terms", value=["legacy-prose"]),
                setting(scope="", key="banned_terms_allowlist", value=["legacy-allowed"]),
                setting(scope="", key="overlay_leak_terms", value=["legacy-overlay"]),
                setting(
                    scope="",
                    key="banned_term_registry",
                    value={"leak": ["registry-brand", "shared"], "tone": ["registry-tone"]},
                ),
                setting(scope="acme", key="banned_terms", value=["overlay-prose"]),
            ]
        )

    def test_legacy_and_registry_rows_merge_without_loss(self) -> None:
        self._seed_legacy_and_registry_rows()

        setting = self._migrate(_AFTER)

        registries = dict(setting.objects.filter(key="banned_term_registry").values_list("scope", "value"))
        assert registries[""] == {
            "leak": ["registry-brand", "shared", "legacy-brand"],
            "prose_collider": ["legacy-prose"],
            "tone": ["registry-tone"],
            "overlay": ["legacy-overlay"],
            "allow": ["legacy-allowed"],
        }
        assert registries["acme"] == {
            "leak": [],
            "prose_collider": ["overlay-prose"],
            "tone": [],
            "overlay": [],
            "allow": [],
        }
        assert not setting.objects.filter(key__in=_LEGACY_KEYS).exists()

    def test_reverse_keeps_the_merged_registry_older_code_reads_first(self) -> None:
        self._seed_legacy_and_registry_rows()
        merged = self._migrate(_AFTER).objects.get(scope="", key="banned_term_registry").value

        setting = self._migrate(_BEFORE)

        assert setting.objects.get(scope="", key="banned_term_registry").value == merged
        assert not setting.objects.filter(key__in=_LEGACY_KEYS).exists()
