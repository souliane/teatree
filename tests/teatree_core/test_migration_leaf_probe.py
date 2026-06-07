"""Pre-merge migration-fork probe (#995).

The §17.4.3 merge gate let through a state that was not end-to-end
mergeable: two branches each added a migration on the same parent, both
reached ``clear+merge`` with CI green, and the fork surfaced only when
the second branch's post-merge ``migrate --no-input`` failed with
``Conflicting migrations detected``. This probe predicts that fork
BEFORE the merge by reading the migration graph of the tree that
``reviewed_sha`` would produce when squash-merged onto the target —
without mutating the worktree (``git merge-tree --write-tree``).

Real ``git init`` under ``tmp_path`` builds the exact symptom: a base
``main`` with one migration, two feature branches each adding a sibling
migration off that same parent. Merging branch B's SHA onto a target
that already carries branch A's migration yields two leaf nodes for the
app — the probe must REJECT. A linear add (a migration whose parent is
the target's current leaf) yields one leaf — the probe must PASS.
"""

import subprocess
from pathlib import Path

import pytest

from teatree.core import migration_leaf_probe as probe_module
from teatree.core.migration_leaf_probe import (
    MigrationLeafConflict,
    _leaves_by_app,
    _merged_tree_oid,
    _migration_blobs,
    _parse_dependencies,
    sha_forks_migration_graph,
)

_BASE_MIGRATION = """\
from django.db import migrations


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = []
"""


def _child_migration(parent: str) -> str:
    return (
        "from django.db import migrations\n\n\n"
        "class Migration(migrations.Migration):\n"
        f'    dependencies = [("core", "{parent}")]\n'
        "    operations = []\n"
    )


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_migration(repo: Path, name: str, body: str) -> None:
    migrations_dir = repo / "src" / "teatree" / "core" / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    init = migrations_dir / "__init__.py"
    if not init.exists():
        init.write_text("")
    (migrations_dir / f"{name}.py").write_text(body)


def _make_remote_with_base_migration(tmp_path: Path) -> Path:
    """Bare remote on ``main`` carrying the initial ``0001`` migration."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(seed, "config", "user.name", "Tester")
    _write_migration(seed, "0001_initial", _BASE_MIGRATION)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "initial migration")

    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(seed), str(bare))
    return bare


def _clone(tmp_path: Path, bare: Path, name: str = "clone") -> Path:
    clone = tmp_path / name
    _git(tmp_path, "clone", str(bare), str(clone))
    _git(clone, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(clone, "config", "user.name", "Tester")
    return clone


def _advance_remote_with_migration(tmp_path: Path, bare: Path, *, name: str, parent: str) -> None:
    """Land a new migration on the remote's ``main`` (simulates branch A merging first)."""
    work = tmp_path / f"advance-{name}"
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(work, "config", "user.name", "Tester")
    _write_migration(work, name, _child_migration(parent))
    _git(work, "add", "-A")
    _git(work, "commit", "-m", f"remote: add {name}")
    _git(work, "push", "origin", "main")


def _feature_branch_with_migration(clone: Path, branch: str, *, name: str, parent: str) -> str:
    _git(clone, "checkout", "-b", branch)
    _write_migration(clone, name, _child_migration(parent))
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", f"feature: add {name}")
    return _git(clone, "rev-parse", "HEAD")


class TestShaForksMigrationGraph:
    def test_rejects_forked_graph_two_migrations_off_same_parent(self, tmp_path: Path) -> None:
        """The #995 symptom: branch B forks off 0001 after branch A's 0002 merged."""
        bare = _make_remote_with_base_migration(tmp_path)
        clone = _clone(tmp_path, bare)
        feature_sha = _feature_branch_with_migration(clone, "feat/b", name="0002_branch_b", parent="0001_initial")
        # Branch A's migration landed on main first — also a child of 0001.
        _advance_remote_with_migration(tmp_path, bare, name="0002_branch_a", parent="0001_initial")

        result = sha_forks_migration_graph(str(clone), feature_sha)

        assert isinstance(result, MigrationLeafConflict)
        assert result.app_label == "core"
        assert result.leaf_count == 2
        assert "0002_branch_a" in result.leaf_names
        assert "0002_branch_b" in result.leaf_names

    def test_passes_linear_graph_migration_chained_off_current_leaf(self, tmp_path: Path) -> None:
        """A migration whose parent is the target's current leaf is linear — one leaf."""
        bare = _make_remote_with_base_migration(tmp_path)
        clone = _clone(tmp_path, bare)
        # Feature chains 0002 off the existing 0001 leaf; main has not moved.
        feature_sha = _feature_branch_with_migration(clone, "feat/x", name="0002_linear", parent="0001_initial")

        assert sha_forks_migration_graph(str(clone), feature_sha) is None

    def test_passes_when_branch_adds_no_migration(self, tmp_path: Path) -> None:
        bare = _make_remote_with_base_migration(tmp_path)
        clone = _clone(tmp_path, bare)
        _git(clone, "checkout", "-b", "feat/nomig")
        (clone / "README.md").write_text("docs only\n")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-m", "feature: docs only")
        feature_sha = _git(clone, "rev-parse", "HEAD")
        _advance_remote_with_migration(tmp_path, bare, name="0002_remote", parent="0001_initial")

        assert sha_forks_migration_graph(str(clone), feature_sha) is None

    def test_does_not_mutate_worktree(self, tmp_path: Path) -> None:
        bare = _make_remote_with_base_migration(tmp_path)
        clone = _clone(tmp_path, bare)
        feature_sha = _feature_branch_with_migration(clone, "feat/b", name="0002_branch_b", parent="0001_initial")
        _advance_remote_with_migration(tmp_path, bare, name="0002_branch_a", parent="0001_initial")
        pre_sha = _git(clone, "rev-parse", "HEAD")

        sha_forks_migration_graph(str(clone), feature_sha)

        assert _git(clone, "rev-parse", "HEAD") == pre_sha
        assert _git(clone, "status", "--porcelain") == ""

    def test_fetch_failure_is_inconclusive_skip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed fetch must not block — same posture as branch_currency/clone_guard."""
        bare = _make_remote_with_base_migration(tmp_path)
        clone = _clone(tmp_path, bare)
        feature_sha = _feature_branch_with_migration(clone, "feat/b", name="0002_branch_b", parent="0001_initial")
        _advance_remote_with_migration(tmp_path, bare, name="0002_branch_a", parent="0001_initial")
        monkeypatch.setattr(probe_module, "_fetch_target", lambda repo, target: False)

        assert sha_forks_migration_graph(str(clone), feature_sha) is None

    def test_unmergeable_tree_is_inconclusive_skip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A merge-tree that cannot be computed (old git / bad object) fails open."""
        bare = _make_remote_with_base_migration(tmp_path)
        clone = _clone(tmp_path, bare)
        feature_sha = _feature_branch_with_migration(clone, "feat/b", name="0002_branch_b", parent="0001_initial")
        _advance_remote_with_migration(tmp_path, bare, name="0002_branch_a", parent="0001_initial")
        monkeypatch.setattr(probe_module, "_merged_tree_oid", lambda repo, reviewed_sha, target: None)

        assert sha_forks_migration_graph(str(clone), feature_sha) is None

    def test_no_migrations_in_tree_is_not_a_fork(self, tmp_path: Path) -> None:
        """A target with no migration files at all yields no finding."""
        seed = tmp_path / "seed"
        seed.mkdir()
        _git(seed, "init", "-b", "main")
        _git(seed, "config", "user.email", "t@e.st")  # privacy-scan:allow
        _git(seed, "config", "user.name", "Tester")
        (seed / "a.txt").write_text("base\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-m", "no migrations")
        bare = tmp_path / "remote.git"
        _git(tmp_path, "clone", "--bare", str(seed), str(bare))
        clone = _clone(tmp_path, bare)
        _git(clone, "checkout", "-b", "feat/x")
        (clone / "b.txt").write_text("feature\n")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-m", "feature")
        feature_sha = _git(clone, "rev-parse", "HEAD")

        assert sha_forks_migration_graph(str(clone), feature_sha) is None


class TestMergedTreeOid:
    def test_empty_merge_tree_output_is_inconclusive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(probe_module, "_git", lambda repo, *args: (0, "   \n"))
        assert _merged_tree_oid("/repo", "sha", "origin/main") is None

    def test_bad_object_return_code_is_inconclusive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(probe_module, "_git", lambda repo, *args: (128, "fatal: bad object"))
        assert _merged_tree_oid("/repo", "sha", "origin/main") is None

    def test_clean_merge_returns_first_line_tree_oid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(probe_module, "_git", lambda repo, *args: (0, "abc123treeoid\n"))
        assert _merged_tree_oid("/repo", "sha", "origin/main") == "abc123treeoid"

    def test_conflict_exit_one_still_returns_merged_tree_oid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """rc=1 (textual conflict) still names the merged tree on its first line."""
        monkeypatch.setattr(probe_module, "_git", lambda repo, *args: (1, "deadbeeftree\nsome/conflicted/path\n"))
        assert _merged_tree_oid("/repo", "sha", "origin/main") == "deadbeeftree"


class TestMigrationBlobs:
    def test_ls_tree_failure_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(probe_module, "_git", lambda repo, *args: (1, ""))
        assert _migration_blobs("/repo", "tree") == {}

    def test_skips_non_migration_and_init_and_malformed_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        out = (
            "100644 blob aaa\tsrc/teatree/core/migrations/0001_initial.py\n"
            "100644 blob bbb\tsrc/teatree/core/migrations/__init__.py\n"
            "100644 blob ccc\tsrc/teatree/core/models/ticket.py\n"
            "garbage-row-no-tab\n"
            "100644 blob\tsrc/teatree/core/migrations/0002_short_meta.py\n"
        )
        monkeypatch.setattr(probe_module, "_git", lambda repo, *args: (0, out))
        blobs = _migration_blobs("/repo", "tree")
        assert blobs == {"core/0001_initial": "aaa"}


class TestLeavesByApp:
    def test_no_blobs_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(probe_module, "_migration_blobs", lambda repo, tree: {})
        assert _leaves_by_app("/repo", "tree") == {}

    def test_cat_file_failure_skips_that_blob_dependencies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            probe_module,
            "_migration_blobs",
            lambda repo, tree: {"core/0001_initial": "aaa", "core/0002_child": "bbb"},
        )

        def _fake_git(repo: str, *args: str) -> tuple[int, str]:
            # cat-file fails for the child blob → its dependency on 0001 is lost,
            # so 0001 is mis-counted as a leaf (the skip branch under test).
            if args[:1] == ("cat-file",) and args[-1] == "bbb":
                return 1, ""
            return 0, ""

        monkeypatch.setattr(probe_module, "_git", _fake_git)
        leaves = _leaves_by_app("/repo", "tree")
        assert sorted(leaves["core"]) == ["0001_initial", "0002_child"]


class TestParseDependencies:
    def test_extracts_app_name_pairs(self) -> None:
        source = 'class Migration:\n    dependencies = [("core", "0001_initial"), ("other", "0003_x")]\n'
        assert _parse_dependencies(source) == [("core", "0001_initial"), ("other", "0003_x")]

    def test_syntax_error_yields_no_pairs(self) -> None:
        assert _parse_dependencies("def (((") == []

    def test_non_literal_and_non_pair_entries_are_skipped(self) -> None:
        source = (
            "class Migration:\n"
            "    dependencies = [\n"
            "        migrations.swappable_dependency(settings.AUTH_USER_MODEL),\n"
            '        ("core", "0001_initial"),\n'
            '        ("core", "x", "extra"),\n'
            "    ]\n"
        )
        assert _parse_dependencies(source) == [("core", "0001_initial")]

    def test_unrelated_list_assignment_ignored(self) -> None:
        assert _parse_dependencies('operations = [("core", "0001_initial")]\n') == []
