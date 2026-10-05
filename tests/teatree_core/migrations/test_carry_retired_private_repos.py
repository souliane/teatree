"""Migration 0124 carries private repositories into the global scope."""

from importlib import import_module

from django.apps import apps
from django.db import connection
from django.test import TestCase

from teatree.core.models import ConfigSetting

_migration = import_module("teatree.core.migrations.0124_delete_rows_of_retired_settings")
carry_retired_values = _migration.carry_retired_values


class TestCarryRetiredPrivateRepos(TestCase):
    def test_canonical_carry_deduplicates_in_order_across_scopes(self) -> None:
        ConfigSetting.objects.create(
            scope="alpha",
            key="internal_publish_namespaces",
            value=["GitLab.COM/Acme/Widget.git", "gitlab.com/acme/widget", "acme/widget", "GITHUB.COM/Acme/Other.GIT/"],
        )
        ConfigSetting.objects.create(scope="", key="private_repos", value=["github.com/acme/existing"])
        ConfigSetting.objects.create(
            scope="beta", key="internal_publish_namespaces", value=["GITHUB.COM/Beta/Repo.git"]
        )

        with self.assertLogs("teatree.core.migrations.0124_delete_rows_of_retired_settings", level="INFO") as captured:
            carry_retired_values(apps, connection.schema_editor())
            carry_retired_values(apps, connection.schema_editor())

        assert ConfigSetting.objects.get(scope="", key="private_repos").value == [
            "github.com/acme/existing",
            "gitlab.com/acme/widget",
            "github.com/acme/other",
            "github.com/beta/repo",
        ]
        assert not ConfigSetting.objects.filter(scope="alpha", key="private_repos").exists()
        assert not ConfigSetting.objects.filter(scope="beta", key="private_repos").exists()
        assert any("1 invalid internal namespace entry/row(s) left unresolved" in line for line in captured.output)

    def test_non_list_private_repos_is_preserved_and_counted(self) -> None:
        ConfigSetting.objects.create(scope="alpha", key="internal_publish_namespaces", value=["github.com/acme/widget"])
        target = ConfigSetting.objects.create(scope="", key="private_repos", value={"operator": "value"})

        with self.assertLogs("teatree.core.migrations.0124_delete_rows_of_retired_settings", level="INFO") as captured:
            carry_retired_values(apps, connection.schema_editor())

        target.refresh_from_db()
        assert target.value == {"operator": "value"}
        assert any("1 source row(s) blocked by non-list global private_repos" in line for line in captured.output)

    def test_unresolved_sources_survive_retirement_for_doctor(self) -> None:
        invalid = ConfigSetting.objects.create(scope="alpha", key="internal_publish_namespaces", value=["bad/repo"])
        blocked = ConfigSetting.objects.create(
            scope="beta", key="internal_publish_namespaces", value=["github.com/beta/repo"]
        )
        ConfigSetting.objects.create(scope="", key="private_repos", value="invalid")

        carry_retired_values(apps, connection.schema_editor())
        _migration.delete_rows(apps, connection.schema_editor())

        assert ConfigSetting.objects.filter(pk=invalid.pk).exists()
        assert ConfigSetting.objects.filter(pk=blocked.pk).exists()
