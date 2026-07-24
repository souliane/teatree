"""``ConfigSetting`` resolves to the canonical DB even from an auto-isolated worktree.

The bug these pin: :func:`teatree.paths.resolve_data_dir` auto-isolates a worktree
checkout onto a per-worktree ``db.sqlite3``, and ``DATABASES["default"]["NAME"]``
follows it — so ``t3 <overlay> config-setting set`` run from a worktree wrote the
operator's setting into a store nothing else reads, while reads came back from a
frozen seeded copy. Config is install-wide, so it must resolve to the one
canonical DB regardless of cwd/worktree.

``default`` stands in for the worktree-local store (it is the non-canonical
connection this process would otherwise write to); a file-backed alias registered
through the shared ``tests.db_alias`` helper stands in for the canonical DB.
``override_settings(DATABASES=...)`` is not usable — Django warns on it and the
suite runs ``filterwarnings = error`` — and the alias must be registered at module
scope so it exists before ``pytest.mark.django_db`` validates its ``databases``.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.db import connections
from django.test import override_settings

from teatree.config.db_router import CONFIG_DB_ALIAS, CONFIG_MODEL_LABEL, ConfigSettingRouter, pinned_config_db
from teatree.core.models import ConfigSetting, Ticket
from tests.db_alias import register_sqlite_alias, teardown_sqlite_alias

_KEY = "provision_ram_ceiling_percent"
_SCOPE = "example-overlay"
_VALUE = 96
_ROUTER_PATH = "teatree.config.db_router.ConfigSettingRouter"


@pytest.fixture(scope="module", autouse=True)
def canonical_db(
    tmp_path_factory: pytest.TempPathFactory,
    django_db_blocker: pytest.FixtureRequest,
) -> Iterator[Path]:
    """Register a real file-backed canonical DB under the router's alias.

    Module-scoped so the alias is in ``connections.databases`` before any
    function-scoped ``django_db`` fixture resolves its ``databases`` list.
    """
    db_file = tmp_path_factory.mktemp("canonical-config") / "db.sqlite3"
    register_sqlite_alias(CONFIG_DB_ALIAS, db_file)
    try:
        with django_db_blocker.unblock(), connections[CONFIG_DB_ALIAS].schema_editor() as editor:
            editor.create_model(ConfigSetting)
        yield db_file
    finally:
        teardown_sqlite_alias(CONFIG_DB_ALIAS)


def _stored_rows(db_file: Path) -> list[tuple[str, str]]:
    """Every ``(scope, key)`` row physically present in *db_file*."""
    conn = sqlite3.connect(db_file)
    try:
        return list(conn.execute("SELECT scope, key FROM teatree_config_setting"))
    finally:
        conn.close()


def _home_with_split_dbs(tmp_path: Path) -> tuple[Path, Path]:
    """A home whose canonical DB and per-worktree isolated DB are distinct files."""
    home = tmp_path / "home"
    canonical = home / ".local" / "share" / "teatree" / "db.sqlite3"
    isolated = home / ".local" / "share" / "teatree-worktrees" / "deadbeef1234" / "db.sqlite3"
    for db in (canonical, isolated):
        db.parent.mkdir(parents=True, exist_ok=True)
    canonical.touch()
    return canonical, isolated


class TestPinnedConfigDb:
    """The settings-time decision: register a second connection, or don't."""

    def test_primary_clone_needs_no_second_connection(self, tmp_path: Path) -> None:
        canonical, _ = _home_with_split_dbs(tmp_path)
        assert pinned_config_db(default_db=canonical, env={}, home=tmp_path / "home") is None

    def test_isolated_worktree_pins_back_to_canonical(self, tmp_path: Path) -> None:
        canonical, isolated = _home_with_split_dbs(tmp_path)
        pinned = pinned_config_db(default_db=isolated, env={}, home=tmp_path / "home")
        assert pinned is not None
        assert pinned.resolve() == canonical.resolve()

    def test_explicit_xdg_sandbox_stays_self_contained(self, tmp_path: Path) -> None:
        """An explicit ``XDG_DATA_HOME`` is a deliberate whole-install sandbox, not a leak."""
        sandbox = tmp_path / "sandbox"
        env = {"XDG_DATA_HOME": str(sandbox)}
        assert pinned_config_db(default_db=sandbox / "teatree" / "db.sqlite3", env=env, home=tmp_path) is None

    def test_absent_canonical_db_registers_nothing(self, tmp_path: Path) -> None:
        # A fresh $HOME with no canonical store (a provisioned test worktree):
        # there is no config to pin, and a connection onto a file in a
        # nonexistent directory crashes Django's connection setup.
        home = tmp_path / "home"
        isolated = home / ".local" / "share" / "teatree-worktrees" / "deadbeef1234" / "db.sqlite3"
        isolated.parent.mkdir(parents=True, exist_ok=True)
        assert pinned_config_db(default_db=isolated, env={}, home=home) is None


class TestConfigSettingRouter:
    """Only ``ConfigSetting`` moves, and only when the alias is registered."""

    def test_model_label_constant_matches_the_real_model(self) -> None:
        assert ConfigSetting._meta.label_lower == CONFIG_MODEL_LABEL

    def test_other_models_are_never_rerouted(self) -> None:
        router = ConfigSettingRouter()
        assert router.db_for_write(ConfigSetting) == CONFIG_DB_ALIAS
        assert router.db_for_read(ConfigSetting) == CONFIG_DB_ALIAS
        assert router.db_for_write(Ticket) is None
        assert router.db_for_read(Ticket) is None

    def test_router_is_inert_when_no_canonical_alias_is_registered(self) -> None:
        """The primary-clone case: one connection, so the router must not reroute."""
        entry = connections.databases.pop(CONFIG_DB_ALIAS)
        try:
            assert ConfigSettingRouter().db_for_write(ConfigSetting) is None
            assert ConfigSettingRouter().db_for_read(ConfigSetting) is None
        finally:
            connections.databases[CONFIG_DB_ALIAS] = entry

    def test_canonical_alias_is_never_migrated(self) -> None:
        """Worktree code carries unmerged migrations; none may reach the canonical DB."""
        router = ConfigSettingRouter()
        assert router.allow_migrate(CONFIG_DB_ALIAS, "core", model_name="configsetting") is False

    def test_the_local_db_still_owns_its_own_schema(self) -> None:
        """Isolation is untouched: the worktree DB is migrated exactly as before."""
        assert ConfigSettingRouter().allow_migrate("default", "core", model_name="configsetting") is None


class TestConfigWritesReachCanonicalFromAWorktree:
    """The end-to-end contract, proved against the canonical sqlite file on disk.

    No ``pytest.mark.django_db``: the marker's test-database setup cannot take an
    alias registered after the session fixture ran, and the point of the fix is
    that ``ConfigSetting`` never touches ``default`` at all — so the ``default``
    connection is deliberately left unopened here rather than transaction-wrapped.
    """

    def test_write_lands_in_the_canonical_file(
        self,
        canonical_db: Path,
        django_db_blocker: pytest.FixtureRequest,
    ) -> None:
        with django_db_blocker.unblock(), override_settings(DATABASE_ROUTERS=[_ROUTER_PATH]):
            ConfigSetting.objects.set_value(_KEY, _VALUE, scope=_SCOPE)
            connections[CONFIG_DB_ALIAS].close()

            assert _stored_rows(canonical_db) == [(_SCOPE, _KEY)]

    def test_read_comes_back_from_canonical(
        self,
        canonical_db: Path,
        django_db_blocker: pytest.FixtureRequest,
    ) -> None:
        assert canonical_db.exists()
        with django_db_blocker.unblock(), override_settings(DATABASE_ROUTERS=[_ROUTER_PATH]):
            ConfigSetting.objects.set_value(_KEY, _VALUE, scope=_SCOPE)

            assert ConfigSetting.objects.get_effective(_KEY, _SCOPE) == _VALUE

    def test_without_the_router_config_resolves_to_the_local_db(self) -> None:
        """Anti-vacuity: strip the router and the manager falls back to ``default``.

        ``default`` is the auto-isolated per-worktree DB in a worktree process, so
        this is exactly the reported bug. If this ever stops resolving to
        ``default``, the cases above are passing for some reason other than the
        router and no longer guard anything.
        """
        with override_settings(DATABASE_ROUTERS=[]):
            assert ConfigSetting.objects.db == "default"

        with override_settings(DATABASE_ROUTERS=[_ROUTER_PATH]):
            assert ConfigSetting.objects.db == CONFIG_DB_ALIAS


class TestProductionSettingsInstallTheRouter:
    """A router nobody registers is a no-op — pin the wiring, not just the class."""

    def test_router_is_registered(self) -> None:
        settings = pytest.importorskip("teatree.settings")
        assert _ROUTER_PATH in settings.DATABASE_ROUTERS
