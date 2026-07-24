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
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.migration import Migration
from django.test import override_settings

from tests.teatree_core._migration_graph import CORE_MIGRATIONS_DIR

_CORE_MAX_MIGRATION_TXT = CORE_MIGRATIONS_DIR / "max_migration.txt"


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


def test_live_core_graph_is_linear_by_dependency() -> None:
    """The live core graph is a single simple chain BY DEPENDENCY — not by numbering.

    Stronger than ``django_linear_migrations``'s dlm.E005 (which only counts leaf
    nodes): a mid-graph diamond (0005 -> 0007, 0005 -> 0006b, both -> 0008) has one
    leaf yet is not linear, so dlm passes while this test would reject it. This is
    the guard the finding asked for — it catches a future renumber-at-merge that
    BRANCHES the graph, without demanding contiguous numbers.

    It asserts NOTHING about the numbers being contiguous. The live graph
    legitimately skips 0006 (0005 -> 0007 -> 0008 — see those migrations' headers),
    and this test stays green on that gap because it walks DEPENDENCIES, not stems.
    """
    loader = MigrationLoader(connection=None, load=False)
    loader.load_disk()
    core = {name: migration for (app, name), migration in loader.disk_migrations.items() if app == "core"}
    assert core, "expected on-disk core migrations to load"

    # A squash migration (non-empty ``replaces``) is a legitimate PARALLEL root: it
    # carries no dependencies and replaces a contiguous range, so it coexists with
    # the still-present original chain until every deployed box is past the squash.
    # It is excluded from the single-chain linearity analysis below (which guards
    # the ORIGINAL chain), but each squash is separately required to be a clean
    # parallel root — so a NON-squash stray root is still caught, not waved through.
    squashes = {name: migration for name, migration in core.items() if migration.replaces}
    for name, migration in squashes.items():
        squash_core_parents = [dep_name for dep_app, dep_name in migration.dependencies if dep_app == "core"]
        assert not squash_core_parents, (
            f"squash {name} must be a parallel root with no core dependencies, got {squash_core_parents}"
        )
    chain_core = {name: migration for name, migration in core.items() if name not in squashes}
    assert chain_core, "expected non-squash core migrations to load"

    # Each migration's dependencies restricted to the core app (a cross-app dep,
    # e.g. on an initial, is not part of the intra-core chain), excluding any dep on
    # a squash so the parallel root never merges into the original-chain analysis.
    def _chain_parents(migration: Migration) -> list[str]:
        return [
            dep_name for dep_app, dep_name in migration.dependencies if dep_app == "core" and dep_name not in squashes
        ]

    core_parents: dict[str, list[str]] = {name: _chain_parents(migration) for name, migration in chain_core.items()}
    # No migration MERGES two core parents.
    for name, parents in core_parents.items():
        assert len(parents) <= 1, f"{name} depends on multiple core migrations {parents} — the graph merges"

    # No parent is depended on by two children — no BRANCH.
    core_children: dict[str, list[str]] = {name: [] for name in chain_core}
    for name, parents in core_parents.items():
        for parent in parents:
            core_children[parent].append(name)
    for parent, children in core_children.items():
        assert len(children) <= 1, f"{parent} is the parent of multiple migrations {children} — the graph branches"

    roots = [name for name, parents in core_parents.items() if not parents]
    leaves = [name for name, children in core_children.items() if not children]
    assert roots == ["0001_initial"], f"expected exactly one non-squash root (0001_initial), got {roots}"
    assert len(leaves) == 1, f"expected exactly one leaf migration, got {leaves}"

    # Walk the unique chain from the root and confirm it visits every non-squash
    # node — a disconnected component would leave a node the walk never reaches.
    chain: list[str] = []
    node: str | None = roots[0]
    while node is not None:
        chain.append(node)
        children = core_children[node]
        node = children[0] if children else None
    assert set(chain) == set(chain_core), (
        "the dependency chain does not cover every core migration — graph is disconnected"
    )
