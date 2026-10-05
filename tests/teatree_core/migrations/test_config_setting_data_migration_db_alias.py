"""The ConfigSetting data migrations write through the schema editor's connection.

``ConfigSettingRouter`` pins every ``ConfigSetting`` read AND write to the canonical
install-wide DB while refusing to migrate that alias — so a router-selected manager
inside a ``RunPython`` body reaches past a worktree's isolation and rewrites rows in
the operator's real config store. The canonical alias here is a second, throwaway
SQLite file carrying only the one table, so a query that consults the router lands
there instead of on the connection being migrated and both halves are observable.
"""

# test-path: cross-cutting -- the subject is teatree.core.migrations (loaded by name below), not teatree.config

import ast
import importlib
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest
from django.apps import apps
from django.db import connection, connections
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import QuerySet
from django.test import TestCase, override_settings

from teatree.config.db_router import CONFIG_MODEL_LABEL
from tests.db_alias import register_sqlite_alias, teardown_sqlite_alias

_MIGRATIONS = (
    "teatree.core.migrations.0027_generic_openai_compatible_backend",
    "teatree.core.migrations.0001_squashed_0030",
)
_IDLE_WINDOW_MIGRATION = "teatree.core.migrations.0099_carry_the_venv_idle_window_onto_every_artifact"
_BANNED_TERM_MIGRATION = "teatree.core.migrations.0120_merge_banned_term_registry"
_OLD_KEY = "orca_router_name"
_NEW_KEY = "openai_compatible_model"


class _PinConfigToCanonical:
    """Stand in for ``ConfigSettingRouter`` — every ConfigSetting query lands on *alias*."""

    def __init__(self, alias: str) -> None:
        self.alias = alias

    def db_for_read(self, model: type, **hints: object) -> str | None:
        _ = hints
        return self.alias if model._meta.label_lower == CONFIG_MODEL_LABEL else None

    db_for_write = db_for_read


@dataclass(frozen=True)
class _StubSchemaEditor:
    """The one attribute a ``RunPython`` body reads off the real schema editor."""

    alias: str

    @property
    def connection(self) -> BaseDatabaseWrapper:
        return connections[self.alias]


@pytest.fixture
def canonical_alias(django_db_blocker: pytest.FixtureRequest) -> Iterator[str]:
    """A private SQLite connection holding only ``teatree_config_setting``.

    Registered after ``setUpClass`` would have installed Django's cross-database
    guards, so the alias stays queryable; the file is thrown away with the fixture.
    """
    alias = f"canon_{uuid.uuid4().hex}"
    with django_db_blocker.unblock(), tempfile.TemporaryDirectory() as tmp:
        register_sqlite_alias(alias, Path(tmp) / f"{alias}.sqlite3")
        with connections[alias].schema_editor() as editor:
            editor.create_model(apps.get_model("core", "ConfigSetting"))
        try:
            yield alias
        finally:
            teardown_sqlite_alias(alias)


class TestConfigSettingDataMigrationUsesTheSchemaEditorConnection(TestCase):
    canonical_alias: str

    @pytest.fixture(autouse=True)
    def _bind_alias(self, canonical_alias: str) -> Iterator[None]:
        self.canonical_alias = canonical_alias
        with override_settings(DATABASE_ROUTERS=[_PinConfigToCanonical(canonical_alias)]):
            yield

    @staticmethod
    def _module(dotted: str) -> ModuleType:
        return importlib.import_module(dotted)

    @staticmethod
    def _migrated() -> QuerySet:
        return apps.get_model("core", "ConfigSetting").objects.using(connection.alias)

    def _canonical(self) -> QuerySet:
        return apps.get_model("core", "ConfigSetting").objects.using(self.canonical_alias)

    def test_forward_carries_the_row_on_the_migrated_connection(self) -> None:
        for dotted in _MIGRATIONS:
            with self.subTest(migration=dotted):
                self._migrated().all().delete()
                self._migrated().create(scope="global", key=_OLD_KEY, value="lane-a")

                self._module(dotted)._carry_configured_values(apps, _StubSchemaEditor(connection.alias))

                assert self._migrated().get(key=_NEW_KEY).value == "lane-a"
                assert not self._migrated().filter(key=_OLD_KEY).exists()

    def test_backward_restores_the_row_on_the_migrated_connection(self) -> None:
        for dotted in _MIGRATIONS:
            with self.subTest(migration=dotted):
                self._migrated().all().delete()
                self._migrated().create(scope="global", key=_NEW_KEY, value="lane-a")

                self._module(dotted)._restore_provider_specific_values(apps, _StubSchemaEditor(connection.alias))

                assert self._migrated().get(key=_OLD_KEY).value == "lane-a"

    def test_the_canonical_config_store_is_never_written(self) -> None:
        self._migrated().all().delete()
        self._migrated().create(scope="global", key=_OLD_KEY, value="lane-a")

        self._module(_MIGRATIONS[0])._carry_configured_values(apps, _StubSchemaEditor(connection.alias))

        assert not self._canonical().filter(key__in=(_OLD_KEY, _NEW_KEY)).exists()

    def test_the_idle_window_rename_uses_the_migrated_connection(self) -> None:
        old_key = "venv_idle_days"
        new_key = "artifact_idle_days"
        self._migrated().all().delete()
        self._migrated().create(scope="global", key=old_key, value="9")

        self._module(_IDLE_WINDOW_MIGRATION).carry_configured_value(
            apps,
            _StubSchemaEditor(connection.alias),
        )

        assert self._migrated().get(key=new_key).value == "9"
        assert not self._canonical().filter(key__in=(old_key, new_key)).exists()

    def test_the_idle_window_reverse_uses_the_migrated_connection(self) -> None:
        old_key = "venv_idle_days"
        new_key = "artifact_idle_days"
        self._migrated().all().delete()
        self._migrated().create(scope="global", key=new_key, value="9")

        self._module(_IDLE_WINDOW_MIGRATION).restore_the_venv_only_key(
            apps,
            _StubSchemaEditor(connection.alias),
        )

        assert self._migrated().get(key=old_key).value == "9"
        assert not self._canonical().filter(key__in=(old_key, new_key)).exists()

    def test_a_router_selected_query_reaches_the_canonical_store(self) -> None:
        self._canonical().create(scope="global", key=_OLD_KEY, value="canonical-only")

        assert apps.get_model("core", "ConfigSetting").objects.get(key=_OLD_KEY).value == "canonical-only"

    def test_registry_merge_updates_only_the_migrated_connection(self) -> None:
        self._migrated().all().delete()
        registry = {"leak": ["existing"]}
        self._canonical().create(pk=7120, scope="global", key="banned_term_registry", value=registry)
        self._migrated().create(pk=7120, scope="global", key="banned_term_registry", value=registry)
        self._migrated().create(scope="global", key="banned_brands", value=["new-term"])

        self._module(_BANNED_TERM_MIGRATION).move_rows(apps, _StubSchemaEditor(connection.alias))

        assert self._migrated().get(pk=7120).value["leak"] == ["existing", "new-term"]
        assert self._canonical().get(pk=7120).value == registry
        assert not self._migrated().filter(key="banned_brands").exists()


def _unscoped_config_queries() -> list[str]:
    """``module:function`` for every migration body that names ConfigSetting yet queries an unscoped manager."""
    import re  # noqa: PLC0415 — only this structural check parses source

    migrations_dir = Path(importlib.import_module("teatree.core.migrations").__file__).parent
    unscoped = re.compile(r"\.objects\.(?!using\()")
    offenders = []
    for path in sorted(migrations_dir.glob("*.py")):
        source = path.read_text()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.FunctionDef):
                continue
            body = ast.get_source_segment(source, node) or ""
            if '"ConfigSetting"' in body and unscoped.search(body):
                offenders.append(f"{path.stem}:{node.name}")
    return offenders


def test_every_config_setting_data_migration_names_its_connection() -> None:
    """The structural half of the two-alias proof, covering migrations the upgrade test never replays."""
    assert _unscoped_config_queries() == []


def _instance_names(function: ast.FunctionDef) -> set[str]:
    names = {
        node.target.id for node in ast.walk(function) if isinstance(node, ast.For) and isinstance(node.target, ast.Name)
    }
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        constructor = node.value.func
        is_instance = (isinstance(constructor, ast.Attribute) and constructor.attr in {"first", "get", "last"}) or (
            isinstance(constructor, ast.Name) and constructor.id in {"min", "max", "next"}
        )
        if is_instance:
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return names


def _unbound_instance_writes(path: Path) -> list[str]:
    offenders = []
    for function in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(function, ast.FunctionDef):
            continue
        names = _instance_names(function)
        for node in ast.walk(function):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"save", "delete"} or not isinstance(node.func.value, ast.Name):
                continue
            if node.func.value.id in names and not any(kw.arg == "using" for kw in node.keywords):
                offenders.append(f"{path.stem}:{function.name}:{node.lineno}")
    return offenders


def test_data_migrations_never_use_an_unbound_instance_write() -> None:
    migrations_dir = Path(importlib.import_module("teatree.core.migrations").__file__).parent
    assert [offender for path in migrations_dir.glob("*.py") for offender in _unbound_instance_writes(path)] == []
