from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

import teatree.config as config_mod
from teatree.config import TeaTreeConfig
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import _discover_toml_overlays

_BEFORE = ("core", "0103_refresh_issue_disposition_description")
_AFTER = ("core", "0104_move_overlay_registry_pass_keys_onto_settings")

type Rows = dict[tuple[str, str], object]


@pytest.mark.timeout(240)
class TestMoveOverlayRegistryPassKeys(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    @staticmethod
    def _seed_before(rows: Rows) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        config_setting = executor.loader.project_state(_BEFORE).apps.get_model("core", "ConfigSetting")
        config_setting.objects.all().delete()
        for (scope, key), value in rows.items():
            config_setting.objects.create(scope=scope, key=key, value=value)

    @staticmethod
    def _migrate_and_read(target: tuple[str, str] = _AFTER) -> Rows:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([target])
        config_setting = executor.loader.project_state(target).apps.get_model("core", "ConfigSetting")
        return {(row.scope, row.key): row.value for row in config_setting.objects.all()}

    @staticmethod
    def _write_after(scope: str, key: str, value: object) -> None:
        executor = MigrationExecutor(connection)
        config_setting = executor.loader.project_state(_AFTER).apps.get_model("core", "ConfigSetting")
        config_setting.objects.filter(scope=scope, key=key).update(value=value, seeded_by="", seed_value=None)

    def test_a_nested_pass_key_becomes_the_overlays_own_setting_row(self) -> None:
        self._seed_before(
            {
                ("", "overlays"): {
                    "acme": {"path": "/srv/acme", "sentry_token_pass_key": "sentry/token"},
                    "other": {"class": "pkg:Overlay"},
                },
            }
        )

        rows = self._migrate_and_read()

        assert rows == {
            ("", "overlays"): {"acme": {"path": "/srv/acme"}, "other": {"class": "pkg:Overlay"}},
            ("acme", "sentry_token_pass_key"): "sentry/token",
        }

    def test_migrated_route_resolves_when_the_registry_overlay_is_discovered(self) -> None:
        self._seed_before(
            {
                ("", "overlays"): {
                    "acme": {
                        "class": "tests.test_overlay_loader:_StubOverlay",
                        "gitlab_token_pass_key": "venue/gitlab",
                    }
                }
            }
        )
        rows = self._migrate_and_read()
        config = TeaTreeConfig(raw={"overlays": rows["", "overlays"]})

        with patch.object(config_mod, "load_config", return_value=config):
            discovered = _discover_toml_overlays(OverlayBase, set())

        resolution = discovered["acme"].config.resolve_pass_key("gitlab_token")
        assert resolution.value == "venue/gitlab"
        assert resolution.source.value == "db, overlay scope"

    def test_a_row_already_set_for_that_overlay_wins(self) -> None:
        self._seed_before(
            {
                ("", "overlays"): {"acme": {"sentry_token_pass_key": "sentry/token"}},
                ("acme", "sentry_token_pass_key"): "venue/sentry",
            }
        )

        rows = self._migrate_and_read()

        assert rows == {("", "overlays"): {"acme": {}}, ("acme", "sentry_token_pass_key"): "venue/sentry"}

    def test_reverse_nests_the_rows_back_into_the_registry(self) -> None:
        self._seed_before({("", "overlays"): {"acme": {"sentry_token_pass_key": "sentry/token"}}})
        self._migrate_and_read()

        rows = self._migrate_and_read(_BEFORE)

        assert rows == {("", "overlays"): {"acme": {"sentry_token_pass_key": "sentry/token"}}}

    def test_reverse_preserves_a_preexisting_scoped_pass_key_row(self) -> None:
        self._seed_before(
            {
                ("", "overlays"): {"acme": {"sentry_token_pass_key": "nested/sentry"}},
                ("acme", "sentry_token_pass_key"): "venue/sentry",
            }
        )
        self._migrate_and_read()

        rows = self._migrate_and_read(_BEFORE)

        assert rows == {
            ("", "overlays"): {"acme": {}},
            ("acme", "sentry_token_pass_key"): "venue/sentry",
        }

    def test_reverse_preserves_a_moved_row_the_operator_changed(self) -> None:
        self._seed_before({("", "overlays"): {"acme": {"sentry_token_pass_key": "nested/sentry"}}})
        self._migrate_and_read()
        self._write_after("acme", "sentry_token_pass_key", "operator/sentry")

        rows = self._migrate_and_read(_BEFORE)

        assert rows == {
            ("", "overlays"): {"acme": {}},
            ("acme", "sentry_token_pass_key"): "operator/sentry",
        }
