"""django-linear-migrations system checks catch forked migration graphs.

``django_linear_migrations`` registers ``check_max_migration_files`` as a
Django system check (tagged ``Tags.models``).  It fires whenever Django runs
its check framework (``python manage.py check``, or any management command
that runs checks).  This suite exercises the check directly against the live
``teatree.core`` migrations directory so that the tests are anti-vacuous:
they fail if ``django_linear_migrations`` is absent from ``INSTALLED_APPS``
or if ``max_migration.txt`` is missing.
"""

from pathlib import Path

import django.conf
from django.core.checks import run_checks
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase

_CORE_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "src" / "teatree" / "core" / "migrations"
_MAX_MIGRATION_TXT = _CORE_MIGRATIONS_DIR / "max_migration.txt"
_SIBLING_LEAF = _CORE_MIGRATIONS_DIR / "0001_sibling_leaf_fork.py"
# A transient linear child of the real leaf — used to manufacture an
# existing-but-stale ``max_migration.txt`` (dlm.E004): it depends on the real
# leaf so the chain stays linear (one leaf), making any earlier name a stale
# entry rather than a fork.
_LINEAR_CHILD = _CORE_MIGRATIONS_DIR / "9999_linear_child.py"


def _real_latest_migration() -> str:
    names = sorted(p.stem for p in _CORE_MIGRATIONS_DIR.glob("[0-9]*.py"))
    return names[-1]


def _core_leaf_dependencies() -> list[tuple[str, str]]:
    """The ``(app, name)`` dependencies of the live ``core`` leaf migration.

    A sibling sharing these dependencies forks the graph (two leaves off the
    same parent). For the squashed single-``0001_initial`` graph the leaf is a
    *root* with no parent, so this returns ``[]`` and the sibling is a second
    root — still two leaf nodes, which is exactly the dlm.E005 fork condition.
    """
    loader = MigrationLoader(None, ignore_no_migrations=True)
    leaf = max(node for node in loader.graph.leaf_nodes() if node[0] == "core")
    return [parent.key for parent in loader.graph.node_map[leaf].parents]


def _migration_source(dependencies: list[tuple[str, str]]) -> str:
    return (
        "from django.db import migrations\n\n\n"
        "class Migration(migrations.Migration):\n"
        f"    dependencies = {dependencies!r}\n"
        "    operations: list = []\n"
    )


class LinearMigrationsCheckTest(SimpleTestCase):
    """check_max_migration_files catches fork signals and passes clean graphs."""

    def setUp(self) -> None:
        self._original_content: str | None = None
        if _MAX_MIGRATION_TXT.exists():
            self._original_content = _MAX_MIGRATION_TXT.read_text()

    def tearDown(self) -> None:
        _SIBLING_LEAF.unlink(missing_ok=True)
        _LINEAR_CHILD.unlink(missing_ok=True)
        if self._original_content is None:
            _MAX_MIGRATION_TXT.unlink(missing_ok=True)
        else:
            _MAX_MIGRATION_TXT.write_text(self._original_content)

    def _dlm_errors(self) -> list[str]:
        errors = run_checks(tags=["models"])
        return [e.id for e in errors if e.id and e.id.startswith("dlm.")]

    def test_linear_graph_passes(self) -> None:
        _MAX_MIGRATION_TXT.write_text(_real_latest_migration() + "\n")
        assert self._dlm_errors() == [], "clean max_migration.txt must produce no dlm.* errors"

    def test_missing_max_migration_txt_raises_e001(self) -> None:
        _MAX_MIGRATION_TXT.unlink(missing_ok=True)
        errors = self._dlm_errors()
        assert "dlm.E001" in errors, f"missing max_migration.txt must yield dlm.E001; got {errors}"

    def test_forked_max_migration_txt_detected(self) -> None:
        latest = _real_latest_migration()
        _MAX_MIGRATION_TXT.write_text(f"{latest}\n0002_other_leaf\n")
        errors = self._dlm_errors()
        assert "dlm.E002" in errors, (
            f"two-line max_migration.txt (merge-conflict residue) must yield dlm.E002; got {errors}"
        )

    def test_stale_max_migration_txt_raises_e004(self) -> None:
        # Add a transient linear child of the REAL leaf so the chain stays
        # linear (one leaf, ``9999_linear_child``); naming the prior real leaf
        # in ``max_migration.txt`` is then an existing — but stale — entry
        # (dlm.E004, not E003), without forking the graph (which would be E005).
        latest = _real_latest_migration()
        _LINEAR_CHILD.write_text(_migration_source([("core", latest)]))
        _MAX_MIGRATION_TXT.write_text(f"{latest}\n")
        errors = self._dlm_errors()
        assert "dlm.E004" in errors, f"stale max_migration.txt must yield dlm.E004; got {errors}"

    def test_multiple_leaf_nodes_raises_e005(self) -> None:
        deps = _core_leaf_dependencies()
        _SIBLING_LEAF.write_text(_migration_source(deps))
        errors = self._dlm_errors()
        assert "dlm.E005" in errors, (
            f"a forked migration graph (a sibling leaf off deps {deps!r}) must yield dlm.E005; got {errors}"
        )

    def test_dlm_installed_in_apps(self) -> None:
        assert "django_linear_migrations" in django.conf.settings.INSTALLED_APPS, (
            "django_linear_migrations must be in INSTALLED_APPS for the check to fire"
        )
