"""Safety-biased incremental test selection (#113, #3672 cutover).

The tach pytest plugin is the impact engine; this module's job is the ESCALATION
policy — :func:`classify_selection` (FULL-vs-scoped + ``--create-db``) and
:func:`build_force_keep` (the floor/reference-reader/mirror/changed-test set force-kept
over the plugin's deselection). The reverse-adjacency graph walk this module used to
re-implement is GONE (#3672) — :class:`TestAdjacencyParsingIsGone` pins that removal so
it cannot silently return.

The binding invariant is unchanged: the selection OVER-selects (force-keeps a superset)
and NEVER under-selects — any change the classifier cannot prove local degrades to a
whole-tree FULL run with the plugin off. The two directions #3817 added are pinned by
:class:`TestSelectionDefiningChangeForcesFull` (the lane cannot scope a change to its
own selection machinery) and :class:`TestLaneRunnerChangeIsScopedNotFull` (a lane runner
nothing imports is mapped to its readers, not escalated).
"""

import inspect
from pathlib import Path
from typing import ClassVar

import pytest

from teatree.quality import affected_tests as mod
from teatree.quality.affected_tests import (
    DEFAULT_BASE,
    FLOOR_DIRS,
    FORCE_KEEP_PLUGIN,
    SELECTION_DEFINING_PATHS,
    ForceKeep,
    Selection,
    SelectionVerdict,
    build_force_keep,
    build_selection,
    classify_selection,
    disk_mirror_lookup,
    is_lane_runner,
    is_reference_mapped,
    module_of,
)
from teatree.quality.changed_set import ChangedSet, ChangedSetError, ChangeEntry

_MERGE_BASE_DOWN = "empty merge-base"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _changed(*entries: tuple[str, str]) -> ChangedSet:
    return ChangedSet(entries=tuple(ChangeEntry(status=s, path=p) for s, p in entries), base_ref="origin/main")


def _force_keep(
    changed: ChangedSet,
    *,
    mirror: dict[str, str] | None = None,
    reference_readers: dict[str, tuple[str, ...]] | None = None,
) -> ForceKeep:
    readers = reference_readers or {}

    def reference_reader_lookup(tokens: frozenset[str]) -> tuple[str, ...]:
        return tuple(sorted({test for token in tokens for test in readers.get(token, ())}))

    return build_force_keep(
        Path("/nonexistent"),
        classify_selection(changed),
        reference_reader_lookup=reference_reader_lookup,
        mirror_lookup=(mirror or {}).get,
    )


class TestModuleOf:
    def test_module_file_maps_to_dotted_module(self) -> None:
        assert module_of("src/teatree/foo/bar.py") == "teatree.foo.bar"

    def test_package_init_maps_to_package_module(self) -> None:
        assert module_of("src/teatree/foo/__init__.py") == "teatree.foo"

    def test_non_src_or_non_python_path_maps_to_none(self) -> None:
        assert module_of("tests/teatree_foo/test_bar.py") is None
        assert module_of("src/teatree/foo/data.yaml") is None


class TestDiskMirrorLookup:
    def _make_module(self, root: Path, *, with_mirror: bool) -> None:
        (root / "src/teatree/foo").mkdir(parents=True)
        (root / "src/teatree/foo/bar.py").write_text("x = 1\n")
        if with_mirror:
            (root / "tests/teatree_foo").mkdir(parents=True)
            (root / "tests/teatree_foo/test_bar.py").write_text("def test_x() -> None: ...\n")

    def test_resolves_a_module_to_its_existing_mirror_test(self, tmp_path: Path) -> None:
        self._make_module(tmp_path, with_mirror=True)
        assert disk_mirror_lookup(tmp_path)("teatree.foo.bar") == "tests/teatree_foo/test_bar.py"

    def test_returns_none_when_the_mirror_file_is_absent(self, tmp_path: Path) -> None:
        self._make_module(tmp_path, with_mirror=False)
        assert disk_mirror_lookup(tmp_path)("teatree.foo.bar") is None


class TestClassifySelection:
    def test_scoped_src_change_is_not_full(self) -> None:
        verdict = classify_selection(_changed(("M", "src/teatree/foo/bar.py")))
        assert not verdict.full
        assert [str(p) for p in verdict.scoped_src] == ["src/teatree/foo/bar.py"]

    def test_changed_test_file_is_scoped_to_itself(self) -> None:
        verdict = classify_selection(_changed(("M", "tests/teatree_foo/test_bar.py")))
        assert not verdict.full
        assert [str(p) for p in verdict.scoped_tests] == ["tests/teatree_foo/test_bar.py"]

    @pytest.mark.parametrize(
        "entry",
        [
            ("M", "src/teatree/foo/corpus.yaml"),  # non-.py data file under src
            ("M", "tests/teatree_foo/fixture.json"),  # non-.py data file under tests
            ("M", "scripts/ci/thing.py"),  # python outside the modelled roots
            ("M", "hooks/scripts/gate.py"),  # hook python outside the graph
            ("M", "src/teatree/foo/notes.md"),  # markdown under a code root is fixture data
        ],
    )
    def test_unclassifiable_change_forces_full(self, entry: tuple[str, str]) -> None:
        verdict = classify_selection(_changed(entry))
        assert verdict.full
        assert verdict.reason

    def test_deletion_forces_full(self) -> None:
        verdict = classify_selection(_changed(("D", "src/teatree/foo/bar.py")))
        assert verdict.full
        assert "delete" in verdict.reason.lower() or "rename" in verdict.reason.lower()

    def test_rename_forces_full(self) -> None:
        assert classify_selection(_changed(("R", "src/teatree/foo/renamed.py"))).full

    def test_conftest_forces_full(self) -> None:
        assert classify_selection(_changed(("M", "tests/teatree_foo/conftest.py"))).full

    def test_factories_forces_full(self) -> None:
        verdict = classify_selection(_changed(("M", "tests/factories.py")))
        assert verdict.full
        assert "factories" in verdict.reason.lower()

    def test_django_settings_forces_full(self) -> None:
        verdict = classify_selection(_changed(("M", "tests/django_settings.py")))
        assert verdict.full
        assert "settings" in verdict.reason.lower()

    def test_tests_config_dir_forces_full(self) -> None:
        assert classify_selection(_changed(("M", "tests/config/test_autonomy.py"))).full

    def test_pyproject_config_forces_full(self) -> None:
        assert classify_selection(_changed(("M", "pyproject.toml"))).full

    def test_migration_forces_full_and_create_db(self) -> None:
        verdict = classify_selection(_changed(("M", "src/teatree/core/migrations/0002_thing.py")))
        assert verdict.full
        assert verdict.create_db

    def test_any_full_trigger_in_a_mixed_diff_forces_full(self) -> None:
        assert classify_selection(_changed(("M", "src/teatree/foo/bar.py"), ("M", "tests/conftest.py"))).full


class TestForceKeepOverSelectsNeverUnder:
    def test_floor_dirs_are_always_force_kept(self) -> None:
        keep = _force_keep(_changed(("M", "src/teatree/foo/bar.py")))
        for floor in FLOOR_DIRS:
            assert floor in keep.paths

    def test_changed_test_file_is_force_kept(self) -> None:
        # Acceptance: a test file at a NON-mirrored path is still caught — it is a
        # changed test, so the force-keep layer runs it even if tach would deselect.
        keep = _force_keep(_changed(("M", "tests/teatree_off/nowhere/test_odd.py")))
        assert "tests/teatree_off/nowhere/test_odd.py" in keep.paths

    def test_changed_src_module_mirror_is_force_kept(self) -> None:
        keep = _force_keep(
            _changed(("M", "src/teatree/foo/bar.py")),
            mirror={"teatree.foo.bar": "tests/teatree_foo/test_bar.py"},
        )
        assert "tests/teatree_foo/test_bar.py" in keep.paths
        assert keep.warnings  # belt-and-braces inclusion is surfaced

    def test_reference_reader_is_force_kept_for_a_changed_doc(self) -> None:
        keep = _force_keep(
            _changed(("M", "BLUEPRINT.md")),
            reference_readers={"BLUEPRINT.md": ("tests/test_blueprint_sync.py",)},
        )
        assert "tests/test_blueprint_sync.py" in keep.paths
        assert [r.kind for r in keep.reasons if r.test == "tests/test_blueprint_sync.py"] == ["reference-read"]

    def test_a_force_kept_test_under_a_floor_dir_is_not_double_listed(self) -> None:
        # A mirror/reference-reader path already under a floor dir is covered by the floor —
        # never listed as an individual path.
        keep = _force_keep(
            _changed(("M", "src/teatree/foo/bar.py")),
            mirror={"teatree.foo.bar": "tests/quality/test_bar_contract.py"},
        )
        assert "tests/quality/test_bar_contract.py" not in keep.paths
        assert "tests/quality" in keep.paths

    def test_a_full_verdict_force_keeps_nothing(self) -> None:
        # FULL never runs the plugin, so its force-keep layer is irrelevant.
        keep = build_force_keep(Path("/nonexistent"), SelectionVerdict(full=True, reason="conftest", create_db=False))
        assert keep.paths == ()
        assert keep.reasons == ()

    def test_force_keep_covers_matches_paths_and_directory_prefixes(self) -> None:
        keep = ForceKeep(paths=("tests/quality", "tests/test_blueprint_sync.py"))
        assert keep.covers("tests/quality/test_x.py")  # under a kept dir
        assert keep.covers("tests/test_blueprint_sync.py")  # exact file
        assert not keep.covers("tests/other/test_y.py")
        assert not keep.covers("tests/quality_helper.py")  # prefix must be a path boundary


class TestPytestArgs:
    def test_full_no_migration_emits_no_positional_args(self) -> None:
        assert Selection(full=True, reason="conftest").pytest_args() == []

    def test_full_migration_emits_create_db(self) -> None:
        assert Selection(full=True, reason="migration", create_db=True).pytest_args() == ["--create-db"]

    def test_scoped_activates_the_plugin_and_pins_the_base(self) -> None:
        sel = Selection(full=False, reason="scoped", base_ref="origin/main")
        args = sel.pytest_args()
        assert "--tach" in args
        assert args[args.index("--tach-base") + 1] == "origin/main"
        assert args[args.index("-p") + 1] == FORCE_KEEP_PLUGIN

    def test_scoped_carries_doctest_parity_for_changed_modules(self) -> None:
        sel = Selection(full=False, reason="scoped", doctest_targets=("src/teatree/foo/bar.py",))
        args = sel.pytest_args()
        assert "--doctest-modules" in args
        assert "src/teatree/foo/bar.py" in args
        # The explicit `tests` root precedes the doctest target so it does not clobber testpaths.
        assert "tests" in args
        assert args.index("tests") < args.index("src/teatree/foo/bar.py")

    def test_scoped_without_changed_modules_needs_no_positional_root(self) -> None:
        # A docs-only scoped diff has no doctest target — testpaths supplies the root.
        args = Selection(full=False, reason="scoped").pytest_args()
        assert "tests" not in args
        assert "--doctest-modules" not in args

    def test_cloned_test_db_swaps_create_db_for_reuse_db(self) -> None:
        # souliane/teatree#3326: once the opt-in template clone refreshed the test DB, a
        # migration diff must NOT replay (--create-db would wipe it) — it reuses instead.
        sel = Selection(full=True, reason="migration", create_db=True)
        assert "--create-db" in sel.pytest_args()
        cloned = sel.pytest_args(test_db_cloned=True)
        assert "--reuse-db" in cloned
        assert "--create-db" not in cloned

    def test_cloned_flag_is_inert_without_create_db(self) -> None:
        sel = Selection(full=False, reason="scoped")
        assert "--reuse-db" not in sel.pytest_args(test_db_cloned=True)
        assert "--create-db" not in sel.pytest_args(test_db_cloned=True)


class TestExplainAndReport:
    def test_explain_traces_a_force_kept_test_reason(self) -> None:
        sel = build_selection_for(
            _changed(("M", "BLUEPRINT.md")),
            reference_readers={"BLUEPRINT.md": ("tests/test_blueprint_sync.py",)},
        )
        chain = sel.explain("tests/test_blueprint_sync.py")
        assert any("names a changed non-imported path" in line for line in chain)

    def test_explain_for_an_unkept_test_says_so(self) -> None:
        sel = build_selection_for(_changed(("M", "src/teatree/foo/bar.py")))
        assert sel.explain("tests/teatree_other/test_nope.py") == [
            (
                "tests/teatree_other/test_nope.py: not force-kept by this diff "
                "(tach selects it only if a changed module reaches it)"
            )
        ]

    def test_full_report_names_the_trigger(self) -> None:
        assert "FULL" in Selection(full=True, reason="conftest tree-wide").report()

    def test_scoped_report_names_the_force_keep_layer(self) -> None:
        report = Selection(full=False, reason="scoped", force_keep=("tests/quality",)).report()
        assert "SCOPED" in report
        assert "force-kept" in report


class TestDocsOnlyChangeIsScopedNotFull:
    """#3645: a docs-only path no longer escalates — its readers are force-kept instead."""

    _BLUEPRINT_READERS: ClassVar[dict[str, tuple[str, ...]]] = {"BLUEPRINT.md": ("tests/test_blueprint_sync.py",)}

    def test_blueprint_edit_alone_does_not_force_full(self) -> None:
        assert not classify_selection(_changed(("M", "BLUEPRINT.md"))).full

    def test_blueprint_edit_force_keeps_the_tests_that_read_it(self) -> None:
        keep = _force_keep(_changed(("M", "BLUEPRINT.md")), reference_readers=self._BLUEPRINT_READERS)
        assert "tests/test_blueprint_sync.py" in keep.paths

    def test_a_doc_with_no_reader_force_keeps_only_the_floor(self) -> None:
        keep = _force_keep(_changed(("M", "docs/blueprint/appendix.md")))
        assert set(keep.paths) == set(FLOOR_DIRS)

    def test_src_change_plus_required_blueprint_edit_stays_scoped(self) -> None:
        # The vicious interaction the ticket names: the blueprint-sync gate COMPELS the
        # doc edit, and that edit used to compel the whole suite.
        verdict = classify_selection(_changed(("M", "src/teatree/foo/bar.py"), ("M", "BLUEPRINT.md")))
        assert not verdict.full
        keep = _force_keep(
            _changed(("M", "src/teatree/foo/bar.py"), ("M", "BLUEPRINT.md")),
            mirror={"teatree.foo.bar": "tests/teatree_foo/test_bar.py"},
            reference_readers=self._BLUEPRINT_READERS,
        )
        assert {"tests/teatree_foo/test_bar.py", "tests/test_blueprint_sync.py"} <= set(keep.paths)

    def test_a_real_full_trigger_beside_a_doc_still_forces_full(self) -> None:
        assert classify_selection(_changed(("M", "BLUEPRINT.md"), ("M", "tests/conftest.py"))).full


class TestSelectionDefiningChangeForcesFull:
    """#3817: the lane refuses to scope a change to the code that computes the selection.

    Before #3817 this fail-safe did not exist for the machinery itself — the shared
    classifier scoped ``src/teatree/quality/changed_set.py`` like any other src module,
    so the selector happily used its own edited logic to decide what to run. The
    ``dev/`` blanket escalation looked like the guard but covered only the shell runner.
    """

    def test_the_classifier_and_the_policy_module_are_both_pinned(self) -> None:
        # Membership is asserted explicitly: the parametrised test below would pass
        # vacuously (zero cases) if the set were emptied.
        assert {
            "src/teatree/quality/changed_set.py",
            "src/teatree/quality/affected_tests.py",
            "dev/test-affected.sh",
        } <= SELECTION_DEFINING_PATHS

    @pytest.mark.parametrize("path", sorted(SELECTION_DEFINING_PATHS))
    def test_every_selection_defining_path_forces_full(self, path: str) -> None:
        verdict = classify_selection(_changed(("M", path)))
        assert verdict.full, f"{path} must force FULL — a selection cannot validate its own change"
        assert "selection" in verdict.reason.lower()

    def test_a_selection_defining_path_beside_a_scopable_one_still_forces_full(self) -> None:
        assert classify_selection(
            _changed(("M", "src/teatree/foo/bar.py"), ("M", "src/teatree/quality/changed_set.py"))
        ).full

    def test_a_selection_defining_path_beside_a_doc_still_forces_full(self) -> None:
        # The doc partition runs BEFORE the shared classifier; the selection-defining
        # check must run before BOTH, or a doc-shaped diff could smuggle one through.
        assert classify_selection(_changed(("M", "BLUEPRINT.md"), ("M", "src/teatree/quality/doc_impact.py"))).full

    def test_the_selection_runner_under_dev_is_not_narrowed_to_a_lane_runner(self) -> None:
        assert not is_lane_runner("dev/test-affected.sh")
        assert not is_reference_mapped("dev/test-affected.sh")

    def test_a_selection_defining_migration_diff_still_asks_for_create_db(self) -> None:
        verdict = classify_selection(
            _changed(("M", "src/teatree/quality/changed_set.py"), ("M", "src/teatree/core/migrations/0002_x.py"))
        )
        assert verdict.full
        assert verdict.create_db

    def test_every_pinned_path_exists_on_disk(self) -> None:
        # Membership is by EXACT path, so a rename that leaves a stale entry silently
        # drops that file out of the fail-safe. This fails on the stale entry instead.
        missing = sorted(path for path in SELECTION_DEFINING_PATHS if not (_REPO_ROOT / path).is_file())
        assert not missing, f"stale SELECTION_DEFINING_PATHS entries (renamed or deleted): {missing}"


class TestLaneRunnerChangeIsScopedNotFull:
    """#3817: a ``dev/`` lane runner is mapped to the tests that NAME it, not escalated.

    Nothing imports a shell / compose / Dockerfile asset and the local suite never
    executes one, so the whole-tree escalation bought no safety and made the lane
    structurally unusable for exactly the change that most needs a local run.
    """

    _PUSH_GATE_READERS: ClassVar[dict[str, tuple[str, ...]]] = {
        "dev/push-gate.sh": ("tests/test_no_full_suite_on_pre_push.py",)
    }

    @pytest.mark.parametrize(
        "path",
        [
            "dev/push-gate.sh",
            "dev/test-shuffle.sh",
            "dev/test-cov.sh",
            "dev/ci-shard.sh",
            "dev/docker-compose.yml",
            "dev/Dockerfile.test",
        ],
    )
    def test_lane_runner_edit_alone_does_not_force_full(self, path: str) -> None:
        verdict = classify_selection(_changed(("M", path)))
        assert not verdict.full, verdict.reason
        assert verdict.scoped_reference_mapped == (path,)

    def test_lane_runner_edit_force_keeps_the_tests_that_name_it(self) -> None:
        keep = _force_keep(_changed(("M", "dev/push-gate.sh")), reference_readers=self._PUSH_GATE_READERS)
        assert "tests/test_no_full_suite_on_pre_push.py" in keep.paths
        kinds = [r.kind for r in keep.reasons if r.test == "tests/test_no_full_suite_on_pre_push.py"]
        assert kinds == ["reference-read"]

    def test_a_deleted_lane_runner_is_still_mapped_to_its_readers(self) -> None:
        # A delete normally forces FULL (stale edges); a non-imported path has no edges,
        # and the tests that named it are exactly the ones that must run.
        verdict = classify_selection(_changed(("D", "dev/push-gate.sh")))
        assert not verdict.full
        keep = _force_keep(_changed(("D", "dev/push-gate.sh")), reference_readers=self._PUSH_GATE_READERS)
        assert "tests/test_no_full_suite_on_pre_push.py" in keep.paths

    def test_python_under_dev_is_importable_and_still_forces_full(self) -> None:
        assert not is_lane_runner("dev/helper.py")
        assert classify_selection(_changed(("M", "dev/helper.py"))).full

    def test_a_real_full_trigger_beside_a_lane_runner_still_forces_full(self) -> None:
        assert classify_selection(_changed(("M", "dev/push-gate.sh"), ("M", "tests/conftest.py"))).full

    def test_config_outside_dev_is_untouched_and_still_forces_full(self) -> None:
        # The narrowing is scoped to `dev/`: the shared classifier's other config
        # triggers (pyproject, .github, .ast-grep) keep escalating exactly as before.
        for path in ("pyproject.toml", ".github/workflows/ci.yml", ".ast-grep/rules/x.yml"):
            assert classify_selection(_changed(("M", path))).full, path


class TestBuildSelectionFailSafe:
    def test_dirty_merge_base_forces_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(base_ref: str, cwd: Path) -> ChangedSet:
            raise ChangedSetError(_MERGE_BASE_DOWN)

        monkeypatch.setattr(mod, "changed_paths", _boom)
        sel = build_selection(Path("/nonexistent"), base_ref="origin/main")
        assert sel.full
        assert "changed set" in sel.reason.lower()

    def test_full_verdict_is_a_plain_whole_suite_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "changed_paths", lambda base_ref, cwd: _changed(("M", "pyproject.toml")))
        sel = build_selection(Path("/nonexistent"), base_ref="origin/main")
        assert sel.full
        assert "--tach" not in sel.pytest_args()  # plugin OFF ⇒ whole suite runs


class TestAdjacencyParsingIsGone:
    """#3672 acceptance: the removed reverse-adjacency code is GONE, not merely bypassed.

    The tach pytest plugin walks the reverse-import graph natively; re-implementing it
    is what this cutover deleted. A grep-assert over the module keeps it deleted.
    """

    _REMOVED_SYMBOLS: ClassVar[tuple[str, ...]] = (
        "run_tach_dependents_map",
        "dependents_closure",
        "scan_test_imports",
        "first_party_imports_deep",
        "SelectionSources",
        "TachUnavailableError",
    )
    _REMOVED_SOURCE_TOKENS: ClassVar[tuple[str, ...]] = (
        "tach map",
        "--direction",
        "dependents",
        "reverse-import closure",
    )

    def test_the_removed_symbols_are_absent_from_the_module(self) -> None:
        for symbol in self._REMOVED_SYMBOLS:
            assert not hasattr(mod, symbol), f"{symbol} must be gone (#3672), not merely bypassed"

    def test_no_tach_subprocess_or_closure_walk_remains_in_the_source(self) -> None:
        source = inspect.getsource(mod)
        for token in self._REMOVED_SOURCE_TOKENS:
            assert token not in source, f"adjacency-parsing token {token!r} still present in affected_tests.py"


def build_selection_for(
    changed: ChangedSet,
    *,
    mirror: dict[str, str] | None = None,
    reference_readers: dict[str, tuple[str, ...]] | None = None,
) -> Selection:
    """A ``build_selection`` assembled from an injected changed set + resolvers (no git/disk)."""
    verdict = classify_selection(changed)
    keep = _force_keep(changed, mirror=mirror, reference_readers=reference_readers)
    changed_src = tuple(str(p) for p in verdict.scoped_src)
    return Selection(
        full=verdict.full,
        reason=verdict.reason,
        create_db=verdict.create_db,
        base_ref=DEFAULT_BASE,
        force_keep=keep.paths,
        doctest_targets=changed_src,
        reasons=keep.reasons,
        changed_src=changed_src,
        changed_tests=tuple(str(p) for p in verdict.scoped_tests),
        changed_reference_mapped=verdict.scoped_reference_mapped,
        warnings=keep.warnings,
    )
