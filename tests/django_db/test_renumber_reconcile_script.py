"""Real-Django integration test for the renumber-reconcile script.

souliane/teatree#1038: ``teatree.utils.django_db.reconcile.RECONCILE_SCRIPT`` runs inside the
target repo's interpreter to rename a snapshot's stale ``django_migrations``
record (the old number a master renumber left behind) so Django's
``check_consistent_history`` passes. The mocked-subprocess tests in
``test_migration.py`` pin the engine's control flow; THIS module pins the
script's actual behaviour against a REAL sqlite ``django_migrations`` table and
a REAL on-disk migration graph — the only check that proves the SQL update and
every hard guard work end to end.

The script is run as a real subprocess against a throwaway Django project under
``tmp_path`` (its own settings, its own sqlite file), exactly the way the engine
invokes it — never imported into this test process, which already has Django
configured for teatree's own settings.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

from teatree.utils.django_db.reconcile import RECONCILE_OK, RECONCILE_SCRIPT, RECONCILE_SKIP


def _write_project(
    root: Path,
    *,
    gadgets_disk: dict[str, list[tuple[str, str]]],
    widgets_dep: tuple[str, str],
    recorded: list[tuple[str, str]],
) -> None:
    """Scaffold a two-app Django project + seed its ``django_migrations`` table.

    *gadgets_disk* maps each on-disk gadgets migration name to its dependency
    list. *widgets_dep* is the gadgets dependency widgets.0016 points at on disk.
    *recorded* is the set of (app, name) rows the snapshot recorded as applied.
    """
    (root / "proj").mkdir(parents=True)
    (root / "proj/__init__.py").write_text("")
    (root / "proj/settings.py").write_text(
        textwrap.dedent(
            f"""
            SECRET_KEY = "x"
            INSTALLED_APPS = ["gadgets", "widgets"]
            DATABASES = {{"default": {{
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": r"{root / "db.sqlite3"}",
            }}}}
            USE_TZ = True
            """
        )
    )

    def _migration(deps: list[tuple[str, str]], *, initial: bool = False) -> str:
        return textwrap.dedent(
            f"""
            from django.db import migrations


            class Migration(migrations.Migration):
                initial = {initial}
                dependencies = {deps!r}
                operations = []
            """
        )

    (root / "gadgets/migrations").mkdir(parents=True)
    (root / "gadgets/__init__.py").write_text("")
    (root / "gadgets/migrations/__init__.py").write_text("")
    (root / "gadgets/migrations/0001_initial.py").write_text(_migration([], initial=True))
    for name, deps in gadgets_disk.items():
        (root / f"gadgets/migrations/{name}.py").write_text(_migration(deps))

    (root / "widgets/migrations").mkdir(parents=True)
    (root / "widgets/__init__.py").write_text("")
    (root / "widgets/migrations/__init__.py").write_text("")
    (root / "widgets/migrations/0001_initial.py").write_text(_migration([], initial=True))
    (root / "widgets/migrations/0016_remove_widgets_owner_link.py").write_text(
        _migration([("widgets", "0001_initial"), widgets_dep])
    )

    seed = textwrap.dedent(
        f"""
        import django
        django.setup()
        from django.db import connection
        from django.db.migrations.recorder import MigrationRecorder
        rec = MigrationRecorder(connection)
        rec.ensure_schema()
        for app, name in {recorded!r}:
            rec.record_applied(app, name)
        """
    )
    _run_py(root, seed)


def _src_dir() -> Path:
    # tests/django_db/<this file> -> repo root -> src
    return Path(__file__).resolve().parents[2] / "src"


def _run_py(root: Path, script: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
    env = {
        "DJANGO_SETTINGS_MODULE": "proj.settings",
        "PYTHONPATH": f"{root}{':'}{_src_dir()}",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        **extra_env,
    }
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _check_consistent(root: Path) -> subprocess.CompletedProcess[str]:
    return _run_py(
        root,
        textwrap.dedent(
            """
            import django
            django.setup()
            from django.db import connection
            from django.db.migrations.loader import MigrationLoader
            from django.db.migrations.exceptions import InconsistentMigrationHistory
            try:
                MigrationLoader(connection).check_consistent_history(connection)
                print("CONSISTENT")
            except InconsistentMigrationHistory as exc:
                print("INCONSISTENT", exc)
            """
        ),
    )


def _records(root: Path, app: str) -> list[str]:
    out = _run_py(
        root,
        textwrap.dedent(
            f"""
            import django
            django.setup()
            from django.db import connection
            from django.db.migrations.recorder import MigrationRecorder
            names = MigrationRecorder(connection).migration_qs.filter(app={app!r}).values_list("name", flat=True)
            print("\\n".join(sorted(names)))
            """
        ),
    )
    return [n for n in out.stdout.splitlines() if n]


class TestReconcileScriptAgainstRealDjango:
    def test_pure_renumber_is_reconciled_and_history_becomes_consistent(self, tmp_path: Path) -> None:
        """The #1038 case: 0046_move… recorded, master renamed it to 0047_move…."""
        root = tmp_path / "repo"
        _write_project(
            root,
            gadgets_disk={
                "0045_add_widget_color": [("gadgets", "0001_initial")],
                "0047_move_owner_link_data": [("gadgets", "0045_add_widget_color")],
            },
            widgets_dep=("gadgets", "0047_move_owner_link_data"),
            recorded=[
                ("gadgets", "0001_initial"),
                ("gadgets", "0045_add_widget_color"),
                ("gadgets", "0046_move_owner_link_data"),  # OLD number
                ("widgets", "0001_initial"),
                ("widgets", "0016_remove_widgets_owner_link"),
            ],
        )

        before = _check_consistent(root)
        assert before.stdout.startswith("INCONSISTENT"), before.stdout + before.stderr

        result = _run_py(
            root,
            "import django; django.setup()\n" + RECONCILE_SCRIPT,
            T3_RECONCILE_DEP_APP="gadgets",
            T3_RECONCILE_DEP_NAME="0047_move_owner_link_data",
        )
        assert RECONCILE_OK in result.stdout, result.stdout + result.stderr

        after = _check_consistent(root)
        assert after.stdout.startswith("CONSISTENT"), after.stdout + after.stderr
        assert _records(root, "gadgets") == [
            "0001_initial",
            "0045_add_widget_color",
            "0047_move_owner_link_data",
        ]

    def test_genuine_divergence_is_not_reconciled(self, tmp_path: Path) -> None:
        """The on-disk dep has a DIFFERENT suffix — a real new migration, not a renumber."""
        root = tmp_path / "repo"
        _write_project(
            root,
            gadgets_disk={
                "0047_genuinely_new_feature": [("gadgets", "0001_initial")],
            },
            widgets_dep=("gadgets", "0047_genuinely_new_feature"),
            recorded=[
                ("gadgets", "0001_initial"),
                ("gadgets", "0046_some_unrelated_old_migration"),
                ("widgets", "0001_initial"),
                ("widgets", "0016_remove_widgets_owner_link"),
            ],
        )

        result = _run_py(
            root,
            "import django; django.setup()\n" + RECONCILE_SCRIPT,
            T3_RECONCILE_DEP_APP="gadgets",
            T3_RECONCILE_DEP_NAME="0047_genuinely_new_feature",
        )
        assert RECONCILE_SKIP in result.stdout, result.stdout + result.stderr
        assert RECONCILE_OK not in result.stdout
        # The stale record must be untouched (real drift surfaced, not masked).
        assert "0046_some_unrelated_old_migration" in _records(root, "gadgets")

    def test_ambiguous_two_stale_records_is_not_reconciled(self, tmp_path: Path) -> None:
        """Two old-numbered records share the dep suffix → ambiguous → refuse."""
        root = tmp_path / "repo"
        _write_project(
            root,
            gadgets_disk={
                "0045_add_widget_color": [("gadgets", "0001_initial")],
                "0047_move_owner_link_data": [("gadgets", "0045_add_widget_color")],
            },
            widgets_dep=("gadgets", "0047_move_owner_link_data"),
            recorded=[
                ("gadgets", "0001_initial"),
                ("gadgets", "0044_move_owner_link_data"),  # ambiguous old #1
                ("gadgets", "0046_move_owner_link_data"),  # ambiguous old #2
                ("widgets", "0001_initial"),
                ("widgets", "0016_remove_widgets_owner_link"),
            ],
        )

        result = _run_py(
            root,
            "import django; django.setup()\n" + RECONCILE_SCRIPT,
            T3_RECONCILE_DEP_APP="gadgets",
            T3_RECONCILE_DEP_NAME="0047_move_owner_link_data",
        )
        assert RECONCILE_SKIP in result.stdout, result.stdout + result.stderr
        assert RECONCILE_OK not in result.stdout
        names = _records(root, "gadgets")
        assert "0044_move_owner_link_data" in names
        assert "0046_move_owner_link_data" in names
