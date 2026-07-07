"""Safety-biased incremental test selection (#113).

The pure core :func:`select` runs on synthetic dependents maps and test-import maps
so the fixture matrix is deterministic and needs no real tach/disk. The binding
invariant across every case: the selection OVER-selects (a superset of the mirror
path unioned with the direct/transitive importers) and NEVER under-selects — any
change the classifier cannot prove local degrades to a whole-tree FULL run.
"""

from pathlib import Path

import pytest

from teatree.quality import affected_tests as mod
from teatree.quality.affected_tests import (
    FLOOR_DIRS,
    Selection,
    TachUnavailableError,
    build_selection,
    classify_selection,
    module_of,
    select,
)
from teatree.quality.changed_set import ChangedSet, ChangedSetError, ChangeEntry

_TACH_DOWN = "tach not found"
_MERGE_BASE_DOWN = "empty merge-base"
_TACH_MUST_NOT_RUN = "tach must not run once the verdict is already FULL"


def _changed(*entries: tuple[str, str]) -> ChangedSet:
    return ChangedSet(entries=tuple(ChangeEntry(status=s, path=p) for s, p in entries), base_ref="origin/main")


def _select(
    changed: ChangedSet,
    *,
    dependents: dict[str, list[str]] | None = None,
    test_imports: dict[str, tuple[str, ...]] | None = None,
    mirror: dict[str, str] | None = None,
) -> Selection:
    return select(
        changed=changed,
        dependents_map=dependents or {},
        test_imports=test_imports or {},
        mirror_lookup=(mirror or {}).get,
        floor_dirs=FLOOR_DIRS,
    )


class TestModuleOf:
    def test_module_file_maps_to_dotted_module(self) -> None:
        assert module_of("src/teatree/foo/bar.py") == "teatree.foo.bar"

    def test_package_init_maps_to_package_module(self) -> None:
        assert module_of("src/teatree/foo/__init__.py") == "teatree.foo"

    def test_non_src_or_non_python_path_maps_to_none(self) -> None:
        assert module_of("tests/teatree_foo/test_bar.py") is None
        assert module_of("src/teatree/foo/data.yaml") is None


class TestOverSelectsNeverUnder:
    def test_changed_src_selects_superset_of_mirror_and_importers(self) -> None:
        changed = _changed(("M", "src/teatree/foo/bar.py"))
        dependents = {
            "src/teatree/foo/bar.py": ["src/teatree/baz.py"],  # baz transitively depends on bar
            "src/teatree/baz.py": [],
        }
        test_imports = {
            "tests/teatree_foo/test_bar.py": ("teatree.foo.bar",),  # direct importer of the change
            "tests/teatree_baz/test_baz.py": ("teatree.baz",),  # importer of the transitive dependent
            "tests/teatree_other/test_other.py": ("teatree.other",),  # unrelated
        }
        mirror = {"teatree.foo.bar": "tests/teatree_foo/test_bar.py"}
        sel = _select(changed, dependents=dependents, test_imports=test_imports, mirror=mirror)

        assert not sel.full
        assert "tests/teatree_foo/test_bar.py" in sel.test_files
        assert "tests/teatree_baz/test_baz.py" in sel.test_files
        # Scoping is meaningful — an unrelated test is not swept in when scoping is provable.
        assert "tests/teatree_other/test_other.py" not in sel.test_files

    def test_ancestor_import_of_changed_module_still_matches(self) -> None:
        # ``from teatree.foo import bar`` records the module as ``teatree.foo`` (the
        # AST-granularity gap). The change is ``teatree.foo.bar`` — the test must still
        # be selected (over-select on the hierarchy, never miss).
        changed = _changed(("M", "src/teatree/foo/bar.py"))
        test_imports = {"tests/teatree_foo/test_bar.py": ("teatree.foo",)}
        sel = _select(changed, test_imports=test_imports)
        assert "tests/teatree_foo/test_bar.py" in sel.test_files

    def test_floor_dirs_present_in_every_scoped_run(self) -> None:
        sel = _select(_changed(("M", "src/teatree/foo/bar.py")))
        assert not sel.full
        assert sel.floor_dirs == FLOOR_DIRS
        for floor in FLOOR_DIRS:
            assert floor in sel.pytest_args()

    def test_mirror_file_included_even_when_import_scan_misses_it(self) -> None:
        changed = _changed(("M", "src/teatree/foo/bar.py"))
        # No test imports the module; only the mirror-path cross-check finds it.
        mirror = {"teatree.foo.bar": "tests/teatree_foo/test_bar.py"}
        sel = _select(changed, test_imports={}, mirror=mirror)
        assert "tests/teatree_foo/test_bar.py" in sel.test_files
        assert sel.warnings  # belt-and-braces inclusion is surfaced

    def test_floor_dir_test_is_not_double_listed(self) -> None:
        changed = _changed(("M", "src/teatree/foo/bar.py"))
        test_imports = {"tests/quality/test_bar_contract.py": ("teatree.foo.bar",)}
        sel = _select(changed, test_imports=test_imports)
        # Already covered by the floor dir — never listed as an individual file arg.
        assert "tests/quality/test_bar_contract.py" not in sel.test_files


class TestChangedTestFileSelectsItself:
    def test_changed_test_file_selects_itself_scoped(self) -> None:
        sel = _select(_changed(("M", "tests/teatree_foo/test_bar.py")))
        assert not sel.full
        assert "tests/teatree_foo/test_bar.py" in sel.test_files
        assert sel.floor_dirs == FLOOR_DIRS


class TestUnclassifiableForcesFull:
    @pytest.mark.parametrize(
        "entry",
        [
            ("M", "src/teatree/foo/corpus.yaml"),  # non-.py data file under src
            ("M", "tests/teatree_foo/fixture.json"),  # non-.py data file under tests
            ("M", "scripts/ci/thing.py"),  # python outside the modelled roots
            ("M", "hooks/scripts/gate.py"),  # hook python outside the graph
            ("M", "e2e/spec/flow.py"),  # e2e python outside the modelled roots
            ("M", "BLUEPRINT.md"),  # doc file a doc-parsing test may read
            ("M", "skills/test/SKILL.md"),  # skill markdown outside the roots
        ],
    )
    def test_unclassifiable_change_forces_full(self, entry: tuple[str, str]) -> None:
        sel = _select(_changed(entry))
        assert sel.full
        assert sel.reason

    def test_deletion_forces_full(self) -> None:
        sel = _select(_changed(("D", "src/teatree/foo/bar.py")))
        assert sel.full
        assert "delete" in sel.reason.lower() or "rename" in sel.reason.lower()

    def test_rename_forces_full(self) -> None:
        assert _select(_changed(("R", "src/teatree/foo/renamed.py"))).full

    def test_conftest_forces_full(self) -> None:
        assert _select(_changed(("M", "tests/teatree_foo/conftest.py"))).full

    def test_factories_forces_full(self) -> None:
        sel = _select(_changed(("M", "tests/factories.py")))
        assert sel.full
        assert "factories" in sel.reason.lower()

    def test_django_settings_forces_full(self) -> None:
        sel = _select(_changed(("M", "tests/django_settings.py")))
        assert sel.full
        assert "settings" in sel.reason.lower()

    def test_tests_config_dir_forces_full(self) -> None:
        assert _select(_changed(("M", "tests/config/test_autonomy.py"))).full

    def test_pyproject_config_forces_full(self) -> None:
        assert _select(_changed(("M", "pyproject.toml"))).full

    def test_migration_forces_full_and_create_db(self) -> None:
        sel = _select(_changed(("M", "src/teatree/core/migrations/0002_thing.py")))
        assert sel.full
        assert sel.create_db
        assert "--create-db" in sel.pytest_args()

    def test_any_full_trigger_in_a_mixed_diff_forces_full(self) -> None:
        # A scopable src change plus one unclassifiable file ⇒ the whole diff is FULL.
        sel = _select(_changed(("M", "src/teatree/foo/bar.py"), ("M", "tests/conftest.py")))
        assert sel.full


class TestExplainChain:
    def test_explain_traces_changed_seed_through_to_the_test(self) -> None:
        changed = _changed(("M", "src/teatree/foo/bar.py"))
        dependents = {"src/teatree/foo/bar.py": ["src/teatree/baz.py"], "src/teatree/baz.py": []}
        test_imports = {"tests/teatree_baz/test_baz.py": ("teatree.baz",)}
        sel = _select(changed, dependents=dependents, test_imports=test_imports)
        chain = next(r.chain for r in sel.reasons if r.test == "tests/teatree_baz/test_baz.py")
        joined = " ".join(chain)
        assert "src/teatree/foo/bar.py" in joined  # the changed seed
        assert "src/teatree/baz.py" in joined  # the transitive dependent
        assert "teatree.baz" in joined  # the import that selected the test

    def test_self_changed_test_reason_is_recorded(self) -> None:
        sel = _select(_changed(("M", "tests/teatree_foo/test_bar.py")))
        reason = next(r for r in sel.reasons if r.test == "tests/teatree_foo/test_bar.py")
        assert reason.kind == "self-changed"

    def test_explain_for_unselected_test_reports_not_selected(self) -> None:
        sel = _select(_changed(("M", "tests/teatree_foo/test_bar.py")))
        assert sel.explain("tests/teatree_other/test_nope.py") == [
            "tests/teatree_other/test_nope.py: not selected by this diff"
        ]


class TestClassifySelection:
    def test_scoped_src_change_is_not_full(self) -> None:
        verdict = classify_selection(_changed(("M", "src/teatree/foo/bar.py")))
        assert not verdict.full
        assert [str(p) for p in verdict.scoped_src] == ["src/teatree/foo/bar.py"]


class TestPytestArgs:
    def test_full_no_migration_emits_no_positional_args(self) -> None:
        assert _select(_changed(("M", "pyproject.toml"))).pytest_args() == []

    def test_scoped_emits_files_then_floor_last(self) -> None:
        changed = _changed(("M", "src/teatree/foo/bar.py"))
        test_imports = {"tests/teatree_foo/test_bar.py": ("teatree.foo.bar",)}
        sel = _select(changed, test_imports=test_imports)
        args = sel.pytest_args()
        assert "tests/teatree_foo/test_bar.py" in args
        assert args[-len(FLOOR_DIRS) :] == list(FLOOR_DIRS)

    def test_scoped_run_carries_doctest_parity_for_changed_modules(self) -> None:
        # Match the CI shard flags: the changed src modules' doctests run locally.
        changed = _changed(("M", "src/teatree/foo/bar.py"))
        test_imports = {"tests/teatree_foo/test_bar.py": ("teatree.foo.bar",)}
        args = _select(changed, test_imports=test_imports).pytest_args()
        assert "--doctest-modules" in args
        assert "src/teatree/foo/bar.py" in args
        # The doctest flag precedes its target module path.
        assert args.index("--doctest-modules") < args.index("src/teatree/foo/bar.py")


class TestBuildSelectionFailSafe:
    def test_tach_unavailable_forces_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "changed_paths", lambda base_ref, cwd: _changed(("M", "src/teatree/foo/bar.py")))

        def _boom(root: Path) -> dict[str, list[str]]:
            raise TachUnavailableError(_TACH_DOWN)

        monkeypatch.setattr(mod, "run_tach_dependents_map", _boom)
        sel = build_selection(Path("/nonexistent"), base_ref="origin/main")
        assert sel.full
        assert "tach" in sel.reason.lower()

    def test_dirty_merge_base_forces_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(base_ref: str, cwd: Path) -> ChangedSet:
            raise ChangedSetError(_MERGE_BASE_DOWN)

        monkeypatch.setattr(mod, "changed_paths", _boom)
        sel = build_selection(Path("/nonexistent"), base_ref="origin/main")
        assert sel.full
        assert "changed set" in sel.reason.lower()

    def test_full_verdict_skips_tach_entirely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "changed_paths", lambda base_ref, cwd: _changed(("M", "pyproject.toml")))

        def _must_not_run(root: Path) -> dict[str, list[str]]:
            raise AssertionError(_TACH_MUST_NOT_RUN)

        monkeypatch.setattr(mod, "run_tach_dependents_map", _must_not_run)
        assert build_selection(Path("/nonexistent"), base_ref="origin/main").full
