"""The eval grading surface is COMPUTED from the import graph, and fails loud when it cannot be.

The gate this feeds (``eval_heal_anticheat``) is default-deny, so the surface is
not what makes a grading module forbidden — it is what proves the gate still
covers every module that computes a verdict, and what refuses an
``EVAL_HARNESS_ALLOWED_PATHS`` entry sitting on the grading call graph.

:class:`TestSyntheticClosure` pins the walk itself against a fixture package:
the three import forms, deferred (in-function) imports, transitivity, and the
exclusion of non-imported siblings. :class:`TestFailLoud` is the anti-vacuity
proof — a renamed seed RAISES instead of silently shrinking the surface, which
is what would make the conformance pin certify nothing.
"""

from pathlib import Path

import pytest

import teatree.eval
from teatree.quality.eval_grading_surface import GRADING_SEEDS, MissingGradingSeedError, grading_surface


def _write_package(root: Path, modules: dict[str, str]) -> Path:
    package = root / "eval"
    package.mkdir()
    for name, source in modules.items():
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    return package


class TestSyntheticClosure:
    def test_seed_alone_is_the_surface_when_it_imports_nothing(self, tmp_path: Path) -> None:
        package = _write_package(tmp_path, {"report.py": "x = 1\n", "renderer.py": "y = 2\n"})
        assert grading_surface(package, seeds=("report.py",)) == frozenset({package / "report.py"})

    def test_from_submodule_import_is_followed(self, tmp_path: Path) -> None:
        package = _write_package(
            tmp_path,
            {"report.py": "from teatree.eval.matchers import assert_tool_call_contains\n", "matchers.py": ""},
        )
        surface = grading_surface(package, seeds=("report.py",))
        assert surface == frozenset({package / "report.py", package / "matchers.py"})

    def test_from_package_import_submodule_is_followed(self, tmp_path: Path) -> None:
        package = _write_package(tmp_path, {"report.py": "from teatree.eval import triage\n", "triage.py": ""})
        surface = grading_surface(package, seeds=("report.py",))
        assert surface == frozenset({package / "report.py", package / "triage.py"})

    def test_plain_import_of_a_submodule_is_followed(self, tmp_path: Path) -> None:
        package = _write_package(tmp_path, {"report.py": "import teatree.eval.judge\n", "judge.py": ""})
        surface = grading_surface(package, seeds=("report.py",))
        assert surface == frozenset({package / "report.py", package / "judge.py"})

    def test_deferred_in_function_import_is_followed(self, tmp_path: Path) -> None:
        # The repo defers imports to break cycles; a walk that reads only
        # module-level imports would drop a grader that is imported inside a call.
        source = "def grade():\n    from teatree.eval.skip_guard import guard\n    return guard\n"
        package = _write_package(tmp_path, {"report.py": source, "skip_guard.py": ""})
        surface = grading_surface(package, seeds=("report.py",))
        assert surface == frozenset({package / "report.py", package / "skip_guard.py"})

    def test_closure_is_transitive(self, tmp_path: Path) -> None:
        package = _write_package(
            tmp_path,
            {
                "report.py": "from teatree.eval.matchers import m\n",
                "matchers.py": "from teatree.eval.models import EvalRun\n",
                "models.py": "",
            },
        )
        surface = grading_surface(package, seeds=("report.py",))
        assert surface == frozenset({package / "report.py", package / "matchers.py", package / "models.py"})

    def test_import_cycle_terminates(self, tmp_path: Path) -> None:
        package = _write_package(
            tmp_path,
            {
                "report.py": "from teatree.eval.triage import t\n",
                "triage.py": "from teatree.eval.report import r\n",
            },
        )
        assert grading_surface(package, seeds=("report.py",)) == frozenset(
            {package / "report.py", package / "triage.py"}
        )

    def test_unimported_sibling_is_not_on_the_surface(self, tmp_path: Path) -> None:
        # ANTI-VACUOUS: the walk excludes something. A surface that swallowed the
        # whole package would make "every grading module is denied" meaningless.
        package = _write_package(tmp_path, {"report.py": "", "summary_markdown.py": ""})
        assert grading_surface(package, seeds=("report.py",)) == frozenset({package / "report.py"})

    def test_foreign_package_import_is_not_followed(self, tmp_path: Path) -> None:
        package = _write_package(tmp_path, {"report.py": "from teatree.core.models import Ticket\n"})
        assert grading_surface(package, seeds=("report.py",)) == frozenset({package / "report.py"})

    def test_imported_name_that_is_not_a_module_is_ignored(self, tmp_path: Path) -> None:
        package = _write_package(tmp_path, {"report.py": "from teatree.eval import EvalSpec\n"})
        assert grading_surface(package, seeds=("report.py",)) == frozenset({package / "report.py"})

    def test_a_subpackage_is_resolved_through_its_init(self, tmp_path: Path) -> None:
        package = _write_package(
            tmp_path, {"report.py": "from teatree.eval.corpus import label\n", "corpus/__init__.py": ""}
        )
        surface = grading_surface(package, seeds=("report.py",))
        assert surface == frozenset({package / "report.py", package / "corpus" / "__init__.py"})

    def test_a_module_inside_a_subpackage_is_resolved(self, tmp_path: Path) -> None:
        package = _write_package(
            tmp_path, {"report.py": "from teatree.eval.corpus.grade import g\n", "corpus/grade.py": ""}
        )
        surface = grading_surface(package, seeds=("report.py",))
        assert surface == frozenset({package / "report.py", package / "corpus" / "grade.py"})

    def test_every_seed_seeds_the_walk(self, tmp_path: Path) -> None:
        package = _write_package(
            tmp_path,
            {"report.py": "", "loader.py": "from teatree.eval.cli_stub_fixture import S\n", "cli_stub_fixture.py": ""},
        )
        surface = grading_surface(package, seeds=("report.py", "loader.py"))
        assert surface == frozenset({package / "report.py", package / "loader.py", package / "cli_stub_fixture.py"})


class TestFailLoud:
    def test_missing_seed_raises_instead_of_shrinking_the_surface(self, tmp_path: Path) -> None:
        package = _write_package(tmp_path, {"report.py": ""})
        with pytest.raises(MissingGradingSeedError) as exc:
            grading_surface(package, seeds=("report.py", "renamed_away.py"))
        assert "renamed_away.py" in str(exc.value)

    def test_missing_package_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(MissingGradingSeedError):
            grading_surface(tmp_path / "absent", seeds=("report.py",))


class TestLiveTree:
    """The real ``teatree.eval`` package — the surface the gate is checked against."""

    @staticmethod
    def _package_dir() -> Path:
        return Path(teatree.eval.__file__).parent

    def test_every_declared_seed_exists(self) -> None:
        surface = grading_surface(self._package_dir())
        assert {path.name for path in surface} >= set(GRADING_SEEDS)

    def test_the_modules_the_ticket_names_are_on_the_surface(self) -> None:
        names = {path.name for path in grading_surface(self._package_dir())}
        assert {"report.py", "loader.py", "skip_guard.py", "summary_json.py", "green_proof.py"} <= names
        assert {"matchers.py", "triage.py", "judge.py", "matcher_vacuity.py"} <= names

    def test_a_pure_renderer_is_not_on_the_surface(self) -> None:
        # ANTI-VACUOUS against the live tree: summary_markdown.py formats an
        # already-decided verdict and reaches no grader, so the computed surface
        # is a real subset of the package, not "every file under it".
        names = {path.name for path in grading_surface(self._package_dir())}
        assert "summary_markdown.py" not in names
