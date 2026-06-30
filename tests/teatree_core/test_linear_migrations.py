"""django-linear-migrations system checks catch forked migration graphs.

``django_linear_migrations`` registers ``check_max_migration_files`` as a
Django system check (tagged ``Tags.models``).  It fires whenever Django runs
its check framework (``python manage.py check``, or any management command
that runs checks).

Each error condition (dlm.E001/E002/E004/E005) is provoked against a throwaway
migrations app built under the test's ``tmp_path`` and installed for the
duration of the check — the live ``teatree.core`` migrations directory is never
mutated.  That isolation is what makes the suite xdist-safe: the live
``max_migration.txt`` is shared mutable state that the system check run by other
suites (e.g. ``call_command`` in ``test_env_command``) reads concurrently, so a
test that overwrote it could race a parallel worker into a spurious dlm.E002.
A separate read-only test asserts the live graph is itself dlm-clean, preserving
the anti-vacuity the per-condition sandbox tests cannot.
"""

import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import django.conf
from django.conf import settings
from django.core.checks import run_checks
from django.test import override_settings

_CORE_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "src" / "teatree" / "core" / "migrations"
_CORE_MAX_MIGRATION_TXT = _CORE_MIGRATIONS_DIR / "max_migration.txt"


def _migration_source(dependencies: list[tuple[str, str]]) -> str:
    return (
        "from django.db import migrations\n\n\n"
        "class Migration(migrations.Migration):\n"
        f"    dependencies = {dependencies!r}\n"
        "    operations: list = []\n"
    )


def _dlm_error_ids() -> list[str]:
    return [e.id for e in run_checks(tags=["models"]) if e.id and e.id.startswith("dlm.")]


@contextmanager
def _sandbox_migrations_app(
    tmp_path: Path,
    *,
    migrations: dict[str, list[str]],
    max_migration: str | None,
) -> Iterator[None]:
    """Install a throwaway migrations app under ``tmp_path`` for the block.

    ``migrations`` maps a migration name to the names of its same-app
    dependencies, so the caller shapes the graph (a linear chain, or two roots
    for a fork) without knowing the app's generated label.  The app is added to
    ``INSTALLED_APPS`` so the real ``check_max_migration_files`` runs against it,
    then fully unwound — including the ``sys.path`` entry and any imported
    submodules — so nothing leaks into a sibling test.  A per-call unique label
    means even a crashed unwind cannot collide a later test on a stale
    ``sys.modules`` entry.
    """
    label = f"dlm_sandbox_{uuid.uuid4().hex}"
    migrations_dir = tmp_path / label / "migrations"
    migrations_dir.mkdir(parents=True)
    (tmp_path / label / "__init__.py").write_text("")
    (migrations_dir / "__init__.py").write_text("")
    for name, sibling_deps in migrations.items():
        (migrations_dir / f"{name}.py").write_text(_migration_source([(label, dep) for dep in sibling_deps]))
    if max_migration is not None:
        (migrations_dir / "max_migration.txt").write_text(max_migration)

    sys.path.insert(0, str(tmp_path))
    try:
        with override_settings(INSTALLED_APPS=[*settings.INSTALLED_APPS, label]):
            yield
    finally:
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))
        for module_name in [m for m in sys.modules if m == label or m.startswith(f"{label}.")]:
            del sys.modules[module_name]


def test_clean_linear_graph_produces_no_dlm_errors(tmp_path: Path) -> None:
    with _sandbox_migrations_app(
        tmp_path,
        migrations={"0001_initial": [], "0002_child": ["0001_initial"]},
        max_migration="0002_child\n",
    ):
        assert _dlm_error_ids() == [], "a clean linear graph must produce no dlm.* errors"


def test_missing_max_migration_txt_raises_e001(tmp_path: Path) -> None:
    with _sandbox_migrations_app(
        tmp_path,
        migrations={"0001_initial": []},
        max_migration=None,
    ):
        errors = _dlm_error_ids()
    assert "dlm.E001" in errors, f"missing max_migration.txt must yield dlm.E001; got {errors}"


def test_forked_max_migration_txt_raises_e002(tmp_path: Path) -> None:
    with _sandbox_migrations_app(
        tmp_path,
        migrations={"0001_initial": []},
        max_migration="0001_initial\n0002_other_leaf\n",
    ):
        errors = _dlm_error_ids()
    assert "dlm.E002" in errors, (
        f"a two-line max_migration.txt (merge-conflict residue) must yield dlm.E002; got {errors}"
    )


def test_stale_max_migration_txt_raises_e004(tmp_path: Path) -> None:
    # A linear chain 0001 -> 0002 with max_migration.txt naming the earlier
    # 0001 — an existing but stale entry (dlm.E004), not a non-existent one
    # (E003) and not a fork (E005).
    with _sandbox_migrations_app(
        tmp_path,
        migrations={"0001_initial": [], "0002_child": ["0001_initial"]},
        max_migration="0001_initial\n",
    ):
        errors = _dlm_error_ids()
    assert "dlm.E004" in errors, f"stale max_migration.txt must yield dlm.E004; got {errors}"


def test_multiple_leaf_nodes_raises_e005(tmp_path: Path) -> None:
    with _sandbox_migrations_app(
        tmp_path,
        migrations={"0001_root_a": [], "0001_root_b": []},
        max_migration="0001_root_a\n",
    ):
        errors = _dlm_error_ids()
    assert "dlm.E005" in errors, f"a forked migration graph (two leaf nodes) must yield dlm.E005; got {errors}"


def test_dlm_installed_and_live_core_graph_is_clean() -> None:
    """Read-only anti-vacuity against the real graph.

    Proves ``django_linear_migrations`` is wired (so the per-condition sandbox
    checks actually fire) and the live ``teatree.core`` max_migration.txt is
    present, single-line, and current.  Reads the live file but never writes it,
    so it stays xdist-safe alongside the system check other suites trigger.
    """
    assert "django_linear_migrations" in django.conf.settings.INSTALLED_APPS, (
        "django_linear_migrations must be in INSTALLED_APPS for the check to fire"
    )
    assert _CORE_MAX_MIGRATION_TXT.exists(), "live core max_migration.txt must exist"
    assert len(_CORE_MAX_MIGRATION_TXT.read_text().strip().splitlines()) == 1, (
        "live core max_migration.txt must be a single line"
    )
    assert _dlm_error_ids() == [], "the live migration graph must be dlm-clean"
