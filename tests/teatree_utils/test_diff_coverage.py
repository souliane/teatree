"""Tests for ``teatree.utils.diff_coverage`` — per-diff coverage + mutation gate.

BLUEPRINT §17.6 gate 12 (#836). The global ``fail_under=93`` masked
untested high-value NEW lines: WS5 / #776 / #800 shipped false "100%
coverage" / "anti-vacuous" claims because a project-wide floor says
nothing about the diff's own new lines. This gate measures coverage on
the *diff's* added/changed production lines (not the global percentage)
and fails if any new line is uncovered.

It also runs a mutation/revert structural check: a new/changed
production symbol must be *referenced by name* from a test file in the
same diff. This catches the "test-a-local-copy" vacuity mechanism a
coverage gate alone cannot — a test that redefines the logic locally and
never imports the shipped symbol can show "100%" while asserting nothing
about production.

Tests use a real ``coverage`` data file built over a temp source tree
and real ``git diff`` output, mocking nothing — the parsing and
intersection logic is exactly what must be exercised.
"""

import shutil
import subprocess
from pathlib import Path
from textwrap import dedent

import coverage
import pytest

from teatree.utils.diff_coverage import (
    CoverageScope,
    DiffCoverageReport,
    added_lines_by_file,
    load_coverage_scope,
    measure_diff_coverage,
    unreferenced_changed_symbols,
)

_GIT = shutil.which("git") or "git"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [_GIT, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _worktree_diff(repo: Path, *pathspec: str) -> str:
    """Diff including untracked files, as the gate receives it in production.

    ``full_worktree_diff`` marks new files intent-to-add before diffing,
    so this mirrors that so the parser sees new files as added hunks.
    """
    _git(repo, "add", "-A", "-N")
    return _git(repo, "diff", "HEAD", "--src-prefix=a/", "--dst-prefix=b/", "--", *pathspec)


def _coverage_db_without(repo: Path) -> Path:
    """A real ``.coverage`` with data — but NOT for the diff's new file.

    Executing ``base.py`` gives the db measured data so coverage does not
    emit the ``no-data-collected`` warning, while the new file under test
    remains genuinely uncovered (the case the gate must catch).
    """
    data_file = repo / ".coverage"
    cov = coverage.Coverage(data_file=str(data_file), source=[str(repo)])
    cov.start()
    ns: dict = {}
    exec(compile((repo / "base.py").read_text(), str(repo / "base.py"), "exec"), ns)  # noqa: S102
    ns["kept"]()
    cov.stop()
    cov.save()
    return data_file


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "base.py").write_text("def kept():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


class TestAddedLinesByFile:
    def test_parses_added_lines_with_resulting_numbers(self, git_repo: Path) -> None:
        (git_repo / "mod.py").write_text("def a():\n    return 1\n\n\ndef b():\n    return 2\n", encoding="utf-8")
        diff = _worktree_diff(git_repo, "mod.py")
        added = added_lines_by_file(diff)
        # New file: every line is added, numbered in the resulting file.
        assert added["mod.py"] == {1, 2, 3, 4, 5, 6}

    def test_modified_file_only_changed_lines(self, git_repo: Path) -> None:
        (git_repo / "base.py").write_text(
            "def kept():\n    return 1\n\n\ndef added():\n    return 9\n", encoding="utf-8"
        )
        diff = _worktree_diff(git_repo, "base.py")
        added = added_lines_by_file(diff)
        # Only the appended ``added()`` lines are new.
        assert added["base.py"] == {3, 4, 5, 6}

    def test_no_added_lines_when_no_diff(self) -> None:
        assert added_lines_by_file("") == {}


class TestMeasureDiffCoverage:
    @staticmethod
    def _coverage_over(repo: Path, covered_module: str) -> Path:
        data_file = repo / ".coverage"
        cov = coverage.Coverage(data_file=str(data_file), source=[str(repo)])
        cov.start()
        ns: dict = {}
        exec(compile((repo / covered_module).read_text(), str(repo / covered_module), "exec"), ns)  # noqa: S102
        ns["new_fn"]()
        cov.stop()
        cov.save()
        return data_file

    def test_fails_when_new_line_is_uncovered(self, git_repo: Path) -> None:
        (git_repo / "feature.py").write_text(
            dedent(
                """\
                def new_fn():
                    return 42
                """
            ),
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo, "feature.py")
        # Build a coverage db where feature.py was never executed.
        data_file = _coverage_db_without(git_repo)
        report = measure_diff_coverage(diff, coverage_data_file=data_file, repo_root=git_repo)
        assert not report.passes()
        assert "feature.py" in {u.path for u in report.uncovered}

    def test_passes_when_all_new_lines_covered(self, git_repo: Path) -> None:
        (git_repo / "feature.py").write_text(
            dedent(
                """\
                def new_fn():
                    return 42
                """
            ),
            encoding="utf-8",
        )
        (git_repo / "tests").mkdir()
        (git_repo / "tests" / "test_feature.py").write_text(
            "from feature import new_fn\n\n\ndef test_it():\n    assert new_fn() == 42\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo)
        data_file = self._coverage_over(git_repo, "feature.py")
        report = measure_diff_coverage(diff, coverage_data_file=data_file, repo_root=git_repo)
        assert report.passes(), (report.uncovered, report.unreferenced_symbols)

    def test_non_python_and_test_files_ignored(self, git_repo: Path) -> None:
        (git_repo / "README.md").write_text("# new docs\n", encoding="utf-8")
        (git_repo / "tests").mkdir()
        (git_repo / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
        diff = _worktree_diff(git_repo)
        data_file = _coverage_db_without(git_repo)
        report = measure_diff_coverage(diff, coverage_data_file=data_file, repo_root=git_repo)
        # Docs + test files are not production lines — nothing to cover.
        assert report.passes()


class TestMutationRevertSymbolCheck:
    """Structural anti-vacuity check.

    A new production symbol must be referenced by name from a test in the
    same diff, or the test could be a local-copy that never exercises it.
    """

    def test_blocks_when_test_redefines_local_copy(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text("def compute_total(x):\n    return x * 2\n", encoding="utf-8")
        (git_repo / "tests").mkdir()
        # The vacuity mechanism: the test defines its OWN copy and never
        # imports the shipped symbol — reverting production cannot make
        # it fail.
        (git_repo / "tests" / "test_shipped.py").write_text(
            "def compute_total(x):\n    return x * 2\n\n\ndef test_it():\n    assert compute_total(2) == 4\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo)
        missing = unreferenced_changed_symbols(diff, repo_root=git_repo)
        assert "compute_total" in missing

    def test_allows_when_test_imports_shipped_symbol(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text("def compute_total(x):\n    return x * 2\n", encoding="utf-8")
        (git_repo / "tests").mkdir()
        (git_repo / "tests" / "test_shipped.py").write_text(
            "from shipped import compute_total\n\n\ndef test_it():\n    assert compute_total(2) == 4\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo)
        missing = unreferenced_changed_symbols(diff, repo_root=git_repo)
        assert missing == set()

    def test_report_combines_both_checks(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text("def widget():\n    return 7\n", encoding="utf-8")
        diff = _worktree_diff(git_repo, "shipped.py")
        data_file = _coverage_db_without(git_repo)
        report = measure_diff_coverage(diff, coverage_data_file=data_file, repo_root=git_repo)
        assert isinstance(report, DiffCoverageReport)
        # Uncovered new line AND no test references the symbol.
        assert not report.passes()
        assert "widget" in report.unreferenced_symbols


class TestSymbolScopeRules:
    """The mutation/revert check targets the public importable API only.

    Private ``_`` helpers (tested via their public callers), framework-
    decorated entrypoints (tested through the framework, not by importing
    the callback by name), and ``typing.Protocol`` classes (a structural
    type contract with no revertible runtime behavior — conformance is
    checked by the type checker, not a test import; souliane/teatree#2888)
    are excluded — they would otherwise false-positive on established
    patterns.
    """

    def test_private_helper_not_required_to_be_referenced(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text(
            "def _helper():\n    return 1\n\n\ndef public_api():\n    return _helper()\n", encoding="utf-8"
        )
        (git_repo / "tests").mkdir()
        (git_repo / "tests" / "test_shipped.py").write_text(
            "from shipped import public_api\n\n\ndef test_it():\n    assert public_api() == 1\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo)
        # _helper is private — exercised through public_api, not required
        # to be named by a test. public_api is imported, so clean.
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()

    def test_decorated_entrypoint_not_required_to_be_referenced(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text(
            "def deco(f):\n    return f\n\n\n@deco\ndef command_cb():\n    return 9\n", encoding="utf-8"
        )
        (git_repo / "tests").mkdir()
        (git_repo / "tests" / "test_shipped.py").write_text(
            "from shipped import deco\n\n\ndef test_it():\n    assert deco(lambda: 9)() == 9\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo)
        # command_cb is decorated (framework-registered) — not required to
        # be imported by name; deco is plain public and is imported.
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()

    def test_nested_function_not_treated_as_top_level_symbol(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text(
            "def outer():\n    def inner():\n        return 1\n    return inner()\n", encoding="utf-8"
        )
        (git_repo / "tests").mkdir()
        (git_repo / "tests" / "test_shipped.py").write_text(
            "from shipped import outer\n\n\ndef test_it():\n    assert outer() == 1\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo)
        # `inner` is nested, not a public importable unit — only `outer`
        # is required, and it is imported.
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()

    def test_protocol_class_not_required_to_be_referenced(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text(
            "from typing import Protocol\n\n\nclass Thing(Protocol):\n    def one(self) -> None: ...\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo, "shipped.py")
        # Thing is a structural type contract (souliane/teatree#2888) — its
        # conformance is checked by the type checker against each concrete
        # implementation, not by a test importing the Protocol by name. No
        # test file changed in this diff at all, and the class is still
        # clean (generalizes the ad-hoc test_harness.py binding-test
        # workaround into the gate itself).
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()

    def test_protocol_class_via_attribute_form_not_required_to_be_referenced(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text(
            "import typing\n\n\nclass Thing(typing.Protocol):\n    def one(self) -> None: ...\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo, "shipped.py")
        # `typing.Protocol` (attribute access form) is recognized the same
        # as `from typing import Protocol`.
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()

    def test_non_protocol_class_still_required_to_be_referenced(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text(
            "class Thing:\n    def one(self) -> None:\n        return None\n", encoding="utf-8"
        )
        diff = _worktree_diff(git_repo, "shipped.py")
        # An ordinary class (no Protocol base) is unaffected by the
        # exemption — still flagged when no changed test references it.
        assert "Thing" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_custom_protocol_subclass_not_recognized_by_name_heuristic(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text(
            "from typing import Protocol\n\n\n"
            "class Base(Protocol):\n    def one(self) -> None: ...\n\n\n"
            "class Thing(Base):\n    def two(self) -> None: ...\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo, "shipped.py")
        # Base is exempt (direct Protocol base), but Thing subclasses Base
        # (not literally named `Protocol`) — the source-level heuristic
        # deliberately does not resolve transitive Protocol inheritance, so
        # Thing still requires a changed-test reference.
        missing = unreferenced_changed_symbols(diff, repo_root=git_repo)
        assert "Base" not in missing
        assert "Thing" in missing

    def test_attribute_form_requires_typing_module_not_any_dot_protocol(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text(
            "import custom\n\n\nclass Thing(custom.Protocol):\n    def one(self) -> None:\n        return None\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo, "shipped.py")
        # `custom.Protocol` is an ordinary attribute access ending in
        # `.Protocol`, but `custom` is not `typing` — an unrelated class
        # merely named `Protocol` must not bypass the anti-vacuity check
        # (review finding on souliane/teatree#2888: a bare `.attr ==
        # "Protocol"` match with no import-provenance check wrongly
        # exempted this).
        assert "Thing" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_name_form_requires_import_from_typing_not_any_protocol_name(self, git_repo: Path) -> None:
        (git_repo / "custom.py").write_text("class Protocol:\n    pass\n", encoding="utf-8")
        (git_repo / "shipped.py").write_text(
            "from custom import Protocol\n\n\n"
            "class Thing(Protocol):\n    def one(self) -> None:\n        return None\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo, "shipped.py", "custom.py")
        # `Protocol` imported from a module other than `typing` is an
        # unrelated symbol that merely shares the name — the bare-name
        # heuristic must resolve import provenance, not just the token.
        missing = unreferenced_changed_symbols(diff, repo_root=git_repo)
        assert "Thing" in missing

    def test_function_local_typing_import_does_not_exempt_module_level_class(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text(
            "def helper() -> None:\n    from typing import Protocol\n    return Protocol\n\n\n"
            "class Thing(Protocol):\n    def one(self) -> None:\n        return None\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo, "shipped.py")
        # `from typing import Protocol` nested inside a function body is not
        # visible at module scope — it must not leak into the bindings used
        # to resolve `Thing`'s top-level `Protocol` base (this file would
        # actually raise NameError at class-definition time, since `Protocol`
        # is never bound at module scope; the gate only ever parses source
        # statically via `ast`, so that is irrelevant here — the point is
        # purely to prove function scoping is respected). Review finding on
        # souliane/teatree#2888: an `ast.walk`-based scan ignored
        # function/class scoping entirely, so this base was wrongly exempted.
        assert "Thing" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_later_reimport_of_protocol_name_removes_typing_binding(self, git_repo: Path) -> None:
        (git_repo / "custom.py").write_text("class Protocol:\n    pass\n", encoding="utf-8")
        (git_repo / "shipped.py").write_text(
            "from typing import Protocol\nfrom custom import Protocol\n\n\n"
            "class Thing(Protocol):\n    def one(self) -> None:\n        return None\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo, "shipped.py", "custom.py")
        # The second import rebinds the local name `Protocol` away from
        # `typing.Protocol` — Python's own name resolution means only the
        # LAST import wins, so `Thing(Protocol)` no longer resolves to a
        # typing Protocol at the point of the class statement (review
        # finding on souliane/teatree#2888).
        missing = unreferenced_changed_symbols(diff, repo_root=git_repo)
        assert "Thing" in missing

    def test_later_reimport_of_typing_alias_removes_binding(self, git_repo: Path) -> None:
        (git_repo / "custom.py").write_text("class Protocol:\n    pass\n", encoding="utf-8")
        (git_repo / "shipped.py").write_text(
            "import typing as t\nimport custom as t\n\n\n"
            "class Thing(t.Protocol):\n    def one(self) -> None:\n        return None\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo, "shipped.py", "custom.py")
        # Same rebinding rule for the module-alias form: the second `import
        # custom as t` shadows the first `import typing as t`, so `t` no
        # longer resolves to the `typing` module at the point `t.Protocol`
        # is used as a base (review finding on souliane/teatree#2888).
        missing = unreferenced_changed_symbols(diff, repo_root=git_repo)
        assert "Thing" in missing

    def test_syntax_error_in_changed_file_is_skipped(self, git_repo: Path) -> None:
        (git_repo / "broken.py").write_text("def x(:\n    pass\n", encoding="utf-8")
        diff = _worktree_diff(git_repo, "broken.py")
        # Unparsable source cannot yield symbols — skipped, not crashed.
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()

    def test_from_import_binding_counts_as_reference(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text("def widget():\n    return 7\n", encoding="utf-8")
        (git_repo / "tests").mkdir()
        # `from shipped import widget` binds the symbol itself. The
        # attribute-access form (`import shipped` + `shipped.widget()`) is
        # covered by TestQualifiedAttributeReferences.
        (git_repo / "tests" / "test_shipped.py").write_text(
            "from shipped import widget\n\n\ndef test_it():\n    assert widget() == 7\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo)
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()


class TestQualifiedAttributeReferences:
    """``import prod`` + ``prod.symbol(...)`` is a genuine production reference.

    The import-only check conflated two shapes. A *bare* ``symbol(...)``
    call is genuinely ambiguous — it resolves to whatever is in scope, so
    a local copy satisfies it and the anti-vacuity refusal is right. A
    *qualified* ``prod.symbol(...)`` call reads through the imported
    module object, so reverting production does turn it red.

    Accepting the qualified shape is narrow by construction, because each
    relaxation is a way to fool the gate: the root must resolve to a
    module path (not a rebound name), the access must be an ``ast.Load``
    (a ``Store`` replaces production rather than reading it), the module
    path must match the changed file's by provenance (not merely share a
    name), and shadowing is ABSOLUTE — a qualified access never cancels a
    local definition of the same name.
    """

    @staticmethod
    def _shipped_with_test(repo: Path, test_source: str) -> str:
        (repo / "shipped.py").write_text("def widget():\n    return 7\n", encoding="utf-8")
        (repo / "tests").mkdir(exist_ok=True)
        (repo / "tests" / "test_shipped.py").write_text(test_source, encoding="utf-8")
        return _worktree_diff(repo)

    def test_module_attribute_call_counts_as_reference(self, git_repo: Path) -> None:
        diff = self._shipped_with_test(
            git_repo,
            "import shipped\n\n\ndef test_it():\n    assert shipped.widget() == 7\n",
        )
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()

    def test_aliased_module_attribute_call_counts_as_reference(self, git_repo: Path) -> None:
        diff = self._shipped_with_test(
            git_repo,
            "import shipped as sh\n\n\ndef test_it():\n    assert sh.widget() == 7\n",
        )
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()

    def test_dotted_module_attribute_call_counts_as_reference(self, git_repo: Path) -> None:
        (git_repo / "pkg").mkdir()
        (git_repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (git_repo / "pkg" / "shipped.py").write_text("def widget():\n    return 7\n", encoding="utf-8")
        (git_repo / "tests").mkdir()
        # `import pkg.shipped` binds only the root name `pkg`; the symbol is
        # reached through the full dotted chain, whose root must still be
        # resolved back to the import.
        (git_repo / "tests" / "test_shipped.py").write_text(
            "import pkg.shipped\n\n\ndef test_it():\n    assert pkg.shipped.widget() == 7\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo)
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()

    def test_from_package_import_module_attribute_counts_as_reference(self, git_repo: Path) -> None:
        (git_repo / "pkg").mkdir()
        (git_repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (git_repo / "pkg" / "shipped.py").write_text("def widget():\n    return 7\n", encoding="utf-8")
        (git_repo / "tests").mkdir()
        (git_repo / "tests" / "test_shipped.py").write_text(
            "from pkg import shipped\n\n\ndef test_it():\n    assert shipped.widget() == 7\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo)
        # `from pkg import shipped` + `shipped.widget()` is the same
        # qualified shape as `import pkg.shipped`, just a different import
        # spelling — a very common one, and it must not be refused.
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()

    def test_from_package_import_module_as_alias_counts_as_reference(self, git_repo: Path) -> None:
        (git_repo / "pkg").mkdir()
        (git_repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (git_repo / "pkg" / "shipped.py").write_text("def widget():\n    return 7\n", encoding="utf-8")
        (git_repo / "tests").mkdir()
        (git_repo / "tests" / "test_shipped.py").write_text(
            "from pkg import shipped as s\n\n\ndef test_it():\n    assert s.widget() == 7\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo)
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()

    def test_from_import_of_an_unrelated_module_is_not_a_reference(self, git_repo: Path) -> None:
        (git_repo / "pkg").mkdir()
        (git_repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (git_repo / "pkg" / "other.py").write_text("def widget():\n    return 0\n", encoding="utf-8")
        _git(git_repo, "add", "-A")
        _git(git_repo, "commit", "-qm", "pkg")
        (git_repo / "pkg" / "shipped.py").write_text("def widget():\n    return 7\n", encoding="utf-8")
        (git_repo / "tests").mkdir()
        (git_repo / "tests" / "test_shipped.py").write_text(
            "from pkg import other\n\n\ndef test_it():\n    assert other.widget() == 0\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo)
        # Same provenance rule as the `import` spelling: mapping the bound
        # name to `pkg.other` is only a CANDIDATE module path, and it does
        # not match the changed `pkg/shipped.py`.
        assert "widget" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_relative_from_import_does_not_match_a_top_level_package(self, git_repo: Path) -> None:
        (git_repo / "pkg").mkdir()
        (git_repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (git_repo / "pkg" / "shipped.py").write_text("def widget():\n    return 7\n", encoding="utf-8")
        (git_repo / "tests").mkdir()
        (git_repo / "tests" / "test_shipped.py").write_text(
            "from .pkg import shipped\n\n\ndef test_it():\n    assert shipped.widget() == 7\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo)
        # The leading dot makes this the TEST package's own `pkg`, not the
        # top-level `pkg/` that changed. Ignoring the dot would let the
        # candidate path collide with an unrelated same-named package.
        assert "widget" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_from_import_still_counts_as_reference(self, git_repo: Path) -> None:
        diff = self._shipped_with_test(
            git_repo,
            "from shipped import widget\n\n\ndef test_it():\n    assert widget() == 7\n",
        )
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()

    def test_qualified_access_does_not_cancel_a_local_definition(self, git_repo: Path) -> None:
        diff = self._shipped_with_test(
            git_repo,
            "import shipped\n\n\ndef widget():\n    return 0\n\n\ndef test_it():\n    assert shipped.widget() == 7\n",
        )
        # SHADOWING IS ABSOLUTE. A test module that ships its own `def
        # widget` AND reaches for production is ambiguous at every bare
        # call site, so no qualified access — call or load — rescues it.
        # Letting a qualified reference cancel shadowing is precisely what
        # let one no-op smoke line launder a whole file; the fix is to give
        # the local helper a different name, not to relax the rule.
        assert "widget" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_bare_call_with_local_copy_still_blocked_even_when_module_imported(self, git_repo: Path) -> None:
        diff = self._shipped_with_test(
            git_repo,
            "import shipped\n\n\ndef widget():\n    return 0\n\n\ndef test_it():\n    assert widget() == 0\n",
        )
        # The vacuity mechanism, with an unused `import shipped` bolted on:
        # the module is imported but the symbol is only ever called BARE, so
        # it resolves to the local copy. Reverting production cannot turn
        # this red — it must stay refused.
        assert "widget" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_bare_call_without_any_import_still_blocked(self, git_repo: Path) -> None:
        diff = self._shipped_with_test(
            git_repo,
            "def test_it():\n    assert widget() == 7\n",
        )
        assert "widget" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_attribute_on_non_module_object_is_not_a_reference(self, git_repo: Path) -> None:
        diff = self._shipped_with_test(
            git_repo,
            "class T:\n    def test_it(self):\n        assert self.widget() == 7\n",
        )
        # `self.widget` is an attribute of a test-local object, not of an
        # imported module — it cannot reach production, so it must not
        # satisfy the gate.
        assert "widget" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_attribute_on_local_variable_is_not_a_reference(self, git_repo: Path) -> None:
        diff = self._shipped_with_test(
            git_repo,
            "def test_it():\n    helper = object()\n    assert helper.widget() == 7\n",
        )
        assert "widget" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_attribute_on_unrelated_imported_module_is_not_a_reference(self, git_repo: Path) -> None:
        # `other.py` is committed in the base, so it is not itself a
        # changed production file — only `shipped.widget` is under test.
        (git_repo / "other.py").write_text("def widget():\n    return 0\n", encoding="utf-8")
        _git(git_repo, "add", "-A")
        _git(git_repo, "commit", "-qm", "other")
        diff = self._shipped_with_test(
            git_repo,
            "import other\n\n\ndef test_it():\n    assert other.widget() == 0\n",
        )
        # `other.widget` cannot turn red when `shipped.widget` is reverted.
        # A qualified access is matched by import provenance: the accessed
        # module path must be a suffix of the changed production file's
        # path, so a same-named symbol on an unrelated module is refused.
        assert "widget" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_stdlib_attribute_does_not_satisfy_a_same_named_production_symbol(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text("def run():\n    return 7\n", encoding="utf-8")
        (git_repo / "tests").mkdir(exist_ok=True)
        (git_repo / "tests" / "test_shipped.py").write_text(
            "import subprocess\n\n\ndef test_it():\n    subprocess.run(['true'], check=False)\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo)
        # Name collisions with the stdlib are ubiquitous — `subprocess.run`
        # appears in ordinary test setup and must never stand in for a
        # changed production `run`.
        assert "run" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_attribute_on_imported_class_is_not_a_module_reference(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text("def cwd():\n    return 7\n", encoding="utf-8")
        (git_repo / "tests").mkdir(exist_ok=True)
        (git_repo / "tests" / "test_shipped.py").write_text(
            "from pathlib import Path\n\n\ndef test_it():\n    assert Path.cwd()\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo)
        # `from pathlib import Path` binds a class, not a module. Reading
        # that class's namespace has no production provenance, so only
        # `import`-bound module names may root a qualified reference.
        assert "cwd" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_attribute_on_a_nested_object_of_the_module_is_not_a_reference(self, git_repo: Path) -> None:
        diff = self._shipped_with_test(
            git_repo,
            "import shipped\n\n\ndef test_it():\n    shipped.registry.widget()\n",
        )
        # `shipped.registry.widget` reads an attribute of an arbitrary
        # object held by the module, not the module's own `widget`.
        assert "widget" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_monkeypatching_the_symbol_away_is_not_a_reference(self, git_repo: Path) -> None:
        diff = self._shipped_with_test(
            git_repo,
            "import shipped\n\n\ndef fake_widget():\n    return 0\n\n\n"
            "def test_it():\n    shipped.widget = fake_widget\n    assert fake_widget() == 0\n",
        )
        # A `Store` attribute REPLACES production with the fake and then
        # asserts on the fake — the canonical vacuity mechanism, not a
        # reference. The fake is deliberately named differently, so the
        # shadowing rule cannot fire and the `ast.Load` guard is the only
        # thing standing between this test and a false pass.
        assert "widget" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_deleting_the_symbol_is_not_a_reference(self, git_repo: Path) -> None:
        diff = self._shipped_with_test(
            git_repo,
            "import shipped\n\n\ndef test_it():\n    del shipped.widget\n    assert not hasattr(shipped, 'widget')\n",
        )
        assert "widget" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_load_only_access_does_not_license_a_local_copy(self, git_repo: Path) -> None:
        diff = self._shipped_with_test(
            git_repo,
            "import shipped\n\n\ndef widget():\n    return 0\n\n\n"
            "def test_smoke():\n    assert shipped.widget is not None\n\n\n"
            "def test_it():\n    assert widget() == 0\n",
        )
        # A lone `assert shipped.widget is not None` smoke line references
        # production but exercises nothing, while every real assertion hits
        # the local copy. The local `def widget` shadows the name, and
        # shadowing is absolute, so the smoke line buys nothing.
        assert "widget" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_load_only_access_counts_when_nothing_shadows_the_name(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text("class Widget:\n    pass\n", encoding="utf-8")
        (git_repo / "tests").mkdir(exist_ok=True)
        (git_repo / "tests" / "test_shipped.py").write_text(
            "import shipped\n\n\ndef test_it():\n    assert isinstance(object(), shipped.Widget) is False\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo)
        # `isinstance(x, prod.Symbol)` and annotations never call the
        # symbol but are genuine references — refusing them would recreate
        # the false positive this whole check exists to remove.
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()

    def test_module_name_rebound_by_assignment_is_not_a_reference(self, git_repo: Path) -> None:
        diff = self._shipped_with_test(
            git_repo,
            "import shipped\nfrom types import SimpleNamespace\n\n"
            "shipped = SimpleNamespace(widget=lambda: 0)\n\n\n"
            "def test_it():\n    assert shipped.widget() == 0\n",
        )
        # The name no longer denotes the imported module, so the access
        # reads a fully local fake.
        assert "widget" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_module_name_shadowed_by_a_fixture_parameter_is_not_a_reference(self, git_repo: Path) -> None:
        diff = self._shipped_with_test(
            git_repo,
            "import shipped\n\n\ndef test_it(shipped):\n    assert shipped.widget() == 0\n",
        )
        # A parameter named after the module wins inside the test body.
        assert "widget" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_setting_an_unrelated_attribute_does_not_rebind_the_module(self, git_repo: Path) -> None:
        diff = self._shipped_with_test(
            git_repo,
            "import shipped\n\n\ndef test_it():\n    shipped._cache = {}\n    assert shipped.widget() == 7\n",
        )
        # `shipped._cache = {}` stores through the module object; the name
        # `shipped` itself is still a Load, so the module is not rebound
        # and the genuine call beside it must still count.
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()


class TestReviewerVacuityProbes:
    """The six probes two reviewers used to measure the gate's blast radius.

    The first cut of the qualified-attribute relaxation accepted ANY
    ``ast.Attribute`` rooted at ANY imported name and let that cancel the
    shadowing rule. Measured baseline-vs-fixed, five of these six leaked
    where the pre-relaxation gate had blocked all six. Each probe is a
    complete, runnable vacuous test: it reaches "100% coverage" while
    reverting production would not turn a single assertion red.

    They are pinned as a set because they fail as a set — every one of
    them is closed by a different rule, and dropping any single rule
    reopens exactly one probe.
    """

    @staticmethod
    def _probe(repo: Path, production: str, test_source: str) -> str:
        (repo / "shipped.py").write_text(production, encoding="utf-8")
        (repo / "tests").mkdir(exist_ok=True)
        (repo / "tests" / "test_shipped.py").write_text(test_source, encoding="utf-8")
        return _worktree_diff(repo)

    def test_probe_a_stdlib_name_collision_blocks(self, git_repo: Path) -> None:
        diff = self._probe(
            git_repo,
            "def run(cmd):\n    return cmd\n",
            "import subprocess\n\n\ndef run(cmd):\n    return cmd\n\n\n"
            "def test_it():\n    assert subprocess.run(['echo'], check=False) is not None\n",
        )
        # Closed by module provenance: `subprocess` is not `shipped`.
        assert "run" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_probe_b_stub_out_blocks(self, git_repo: Path) -> None:
        diff = self._probe(
            git_repo,
            "def widget():\n    return 0\n",
            "import shipped\n\n\ndef widget():\n    return 1\n\n\n"
            "shipped.widget = widget\n\n\ndef test_it():\n    assert widget() == 1\n",
        )
        # Closed by the `ast.Load` guard: a `Store` replaces production.
        assert "widget" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_probe_c_unreachable_qualified_call_blocks(self, git_repo: Path) -> None:
        diff = self._probe(
            git_repo,
            "def widget():\n    return 0\n",
            "import shipped\n\n\ndef widget():\n    return 1\n\n\n"
            "def test_it():\n    if False:\n        shipped.widget()\n    assert widget() == 1\n",
        )
        # Closed by absolute shadowing, NOT by reachability analysis — the
        # check stays syntactic. A dead-branch qualified call with no local
        # copy is still credited here; the line-coverage half is what
        # refuses a reference that never executes.
        assert "widget" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_probe_d_attribute_on_imported_class_blocks(self, git_repo: Path) -> None:
        diff = self._probe(
            git_repo,
            "def cwd():\n    return 0\n",
            "from pathlib import Path\n\n\ndef cwd():\n    return 1\n\n\n"
            "def test_it():\n    assert Path.cwd() is not None\n",
        )
        # Closed by module provenance: `pathlib.Path` is a class namespace.
        assert "cwd" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_probe_e_pytest_name_collision_blocks(self, git_repo: Path) -> None:
        diff = self._probe(
            git_repo,
            "def raises(x):\n    return x\n",
            "import pytest\n\n\ndef raises(x):\n    return x\n\n\n"
            "def test_it():\n    with pytest.raises(ValueError):\n        raise ValueError\n",
        )
        # Closed by module provenance. `pytest.raises` is in essentially
        # every test file, so a bare-name match here would be catastrophic.
        assert "raises" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_probe_control_no_import_at_all_blocks(self, git_repo: Path) -> None:
        diff = self._probe(
            git_repo,
            "def widget():\n    return 0\n",
            "def widget():\n    return 1\n\n\ndef test_it():\n    assert widget() == 1\n",
        )
        # The original anti-vacuity property, unchanged: a bare `widget()`
        # with no import of `widget` resolves to the local copy.
        assert "widget" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_one_no_op_smoke_line_cannot_launder_a_file(self, git_repo: Path) -> None:
        diff = self._probe(
            git_repo,
            "def widget():\n    return 0\n",
            "import shipped\n\n\ndef widget():\n    return 0\n\n\n"
            "def test_symbol_exists():\n    assert shipped.widget is not None\n\n\n"
            "def test_behaviour():\n    assert widget() == 0\n",
        )
        # The exploit the reviewers derived from the root cause: every real
        # assertion hits the local copy while a single no-op line supplies
        # the "reference". Absolute shadowing is what kills it.
        assert "widget" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_legitimate_qualified_call_still_passes(self, git_repo: Path) -> None:
        diff = self._probe(
            git_repo,
            "def widget():\n    return 7\n",
            "import shipped\n\n\ndef test_it():\n    assert shipped.widget() == 7\n",
        )
        # The false positive the relaxation exists to remove. Tightening
        # the rule must not take this back down with it.
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()

    def test_legitimate_class_member_read_still_passes(self, git_repo: Path) -> None:
        diff = self._probe(
            git_repo,
            "import enum\n\n\nclass Lookup(enum.Enum):\n    FAILED = 1\n    ABSENT = 2\n",
            "import shipped\n\n\ndef fake(kind) -> str | shipped.Lookup:\n"
            "    if kind == 'bad':\n        return shipped.Lookup.FAILED\n    return 'ok'\n\n\n"
            "def test_it():\n    assert fake('bad') is shipped.Lookup.FAILED\n"
            "    assert fake('good') == 'ok'\n",
        )
        # `prod.Enum.MEMBER` and `x: prod.Type` are never CALLS, but they
        # read through the module object and go red on revert. Requiring a
        # call would reject this shape — a real one, and the reason the
        # call-only narrowing was declined.
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()


class TestSummaryRendering:
    def test_clean_summary_text(self) -> None:
        assert "clean" in DiffCoverageReport().summary()

    def test_failed_summary_lists_findings(self) -> None:
        from teatree.utils.diff_coverage import UncoveredFile  # noqa: PLC0415

        report = DiffCoverageReport(
            uncovered=[UncoveredFile(path="src/x.py", lines=[3, 4])],
            unreferenced_symbols=["widget"],
        )
        text = report.summary()
        assert "FAILED" in text
        assert "src/x.py" in text
        assert "widget" in text


class TestFreshAnalysisAndEdgeCases:
    def test_file_never_imported_under_coverage_all_new_lines_uncovered(self, git_repo: Path) -> None:
        # feature.py is in scope but the .coverage db has no record of it
        # at all (it was never imported). Every executable added line is
        # then uncovered via a fresh source analysis.
        (git_repo / "feature.py").write_text("def fn():\n    return 1\n", encoding="utf-8")
        (git_repo / "tests").mkdir()
        (git_repo / "tests" / "test_feature.py").write_text(
            "from feature import fn\n\n\ndef test_it():\n    assert fn() == 1\n", encoding="utf-8"
        )
        diff = _worktree_diff(git_repo)
        data_file = _coverage_db_without(git_repo)
        report = measure_diff_coverage(diff, coverage_data_file=data_file, repo_root=git_repo)
        assert not report.passes()
        assert "feature.py" in {u.path for u in report.uncovered}

    def test_unparseable_source_for_fresh_analysis_treats_all_added_as_uncovered(self, git_repo: Path) -> None:
        from teatree.utils.diff_coverage import _uncovered_via_fresh_analysis  # noqa: PLC0415

        class _RaisingCov:
            def analysis2(self, _path: str) -> tuple:
                msg = "NoSource"
                raise RuntimeError(msg)

        # When coverage cannot analyse the file, fail closed: every added
        # line is reported uncovered rather than silently passing.
        assert _uncovered_via_fresh_analysis(_RaisingCov(), "/nope.py", {1, 2, 3}) == [1, 2, 3]

    def test_test_file_syntax_error_is_skipped_for_symbol_check(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text("def widget():\n    return 7\n", encoding="utf-8")
        (git_repo / "tests").mkdir()
        (git_repo / "tests" / "test_broken.py").write_text("def t(:\n  pass\n", encoding="utf-8")
        diff = _worktree_diff(git_repo)
        # The unparsable test file is skipped; widget is then genuinely
        # unreferenced (no valid test imports it).
        assert "widget" in unreferenced_changed_symbols(diff, repo_root=git_repo)

    def test_no_coverage_db_still_runs_symbol_check_only(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text("def widget():\n    return 7\n", encoding="utf-8")
        diff = _worktree_diff(git_repo, "shipped.py")
        report = measure_diff_coverage(diff, coverage_data_file=git_repo / "absent.coverage", repo_root=git_repo)
        # No .coverage ⇒ no line findings, but the structural symbol
        # check still runs and flags the unreferenced symbol.
        assert report.uncovered == []
        assert "widget" in report.unreferenced_symbols


class TestParserAndDefensiveBranches:
    def test_modified_file_with_deletions_only_counts_added(self, git_repo: Path) -> None:
        # Replacing content produces both `-` and `+` hunk lines; the
        # parser must skip removed lines and count only the post-image
        # added ones.
        (git_repo / "base.py").write_text("def kept():\n    return 2\n", encoding="utf-8")
        diff = _worktree_diff(git_repo, "base.py")
        added = added_lines_by_file(diff)
        assert added["base.py"] == {2}

    def test_module_level_assignment_is_not_a_symbol(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text("CONST = 1\n\n\ndef widget():\n    return CONST\n", encoding="utf-8")
        (git_repo / "tests").mkdir()
        (git_repo / "tests" / "test_shipped.py").write_text(
            "from shipped import widget\n\n\ndef test_it():\n    assert widget() == 1\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo)
        # CONST is an assignment, not a def/class — never a required
        # symbol; widget is imported, so clean.
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()

    def test_plain_import_statement_binds_top_level_name(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text("def widget():\n    return 7\n", encoding="utf-8")
        (git_repo / "tests").mkdir()
        # A test that does `import os` (plain Import node) alongside the
        # canonical `from shipped import widget` — exercises the Import
        # branch of the AST walk.
        (git_repo / "tests" / "test_shipped.py").write_text(
            "import os\nfrom shipped import widget\n\n\ndef test_it():\n    assert widget() == 7 and os\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo)
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()

    def test_changed_test_file_absent_on_disk_is_skipped(self, git_repo: Path) -> None:
        # A diff naming a *test* file that no longer exists on disk must
        # be skipped by the symbol-reference loop, not crash. shipped.py
        # is real and unreferenced, so it is still flagged.
        (git_repo / "shipped.py").write_text("def widget():\n    return 7\n", encoding="utf-8")
        real = _worktree_diff(git_repo, "shipped.py")
        ghost = (
            "diff --git a/tests/test_ghost.py b/tests/test_ghost.py\n"
            "--- /dev/null\n"
            "+++ b/tests/test_ghost.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+from shipped import widget\n"
        )
        missing = unreferenced_changed_symbols(real + ghost, repo_root=git_repo)
        assert "widget" in missing

    def test_in_scope_file_never_measured_uses_fresh_analysis(self, git_repo: Path) -> None:
        # pyproject scopes coverage to the whole repo; the db has data
        # (base.py) but feature.py was never measured ⇒ the `actual is
        # None` fresh-analysis branch reports its added lines uncovered.
        (git_repo / "pyproject.toml").write_text('[tool.coverage.run]\nsource = ["src"]\n', encoding="utf-8")
        (git_repo / "src").mkdir()
        (git_repo / "src" / "feature.py").write_text("def fn():\n    return 1\n", encoding="utf-8")
        (git_repo / "tests").mkdir()
        (git_repo / "tests" / "test_feature.py").write_text(
            "from feature import fn\n\n\ndef test_it():\n    assert fn() == 1\n", encoding="utf-8"
        )
        diff = _worktree_diff(git_repo)
        data_file = _coverage_db_without(git_repo)
        report = measure_diff_coverage(diff, coverage_data_file=data_file, repo_root=git_repo)
        assert "src/feature.py" in {u.path for u in report.uncovered}

    def test_changed_file_absent_on_disk_is_skipped(self, git_repo: Path) -> None:
        # A diff that references a file which no longer exists on disk
        # (e.g. created then removed) must be skipped, not crash.
        diff = (
            "diff --git a/gone.py b/gone.py\n"
            "--- /dev/null\n"
            "+++ b/gone.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+def ghost():\n"
            "+    return 1\n"
        )
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()

    def test_fresh_analysis_success_path_on_unmeasured_in_scope_file(self, git_repo: Path) -> None:
        from teatree.utils.diff_coverage import _uncovered_via_fresh_analysis  # noqa: PLC0415

        target = git_repo / "fresh.py"
        target.write_text("def fn():\n    return 1\n", encoding="utf-8")
        data_file = _coverage_db_without(git_repo)
        cov = coverage.Coverage(data_file=str(data_file))
        cov.load()
        # fresh.py was never measured: every executable added line (the
        # `def` line and the return) is reported uncovered through the
        # success path.
        assert _uncovered_via_fresh_analysis(cov, str(target), {1, 2}) == [1, 2]


class TestCoverageScope:
    """The gate only enforces files the project's coverage config measures.

    Subprocess-only scripts (``scripts/``, ``hooks/``) are outside
    ``[tool.coverage.run] source`` and so out of scope for *line*
    coverage — exactly as the existing global ``fail_under`` gate treats
    them — instead of demanding impossible coverage.
    """

    def test_load_reads_source_and_omit(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.coverage.run]\nsource = ["src/teatree"]\nomit = ["src/teatree/core/migrations/*.py"]\n',
            encoding="utf-8",
        )
        scope = load_coverage_scope(tmp_path / "pyproject.toml")
        assert scope.source_roots == ("src/teatree",)
        assert scope.includes("src/teatree/utils/x.py")
        assert not scope.includes("scripts/foo.py")
        assert not scope.includes("src/teatree/core/migrations/0001.py")

    def test_missing_pyproject_includes_everything(self, tmp_path: Path) -> None:
        scope = load_coverage_scope(tmp_path / "pyproject.toml")
        assert scope.includes("anything/at/all.py")

    def test_out_of_scope_script_not_flagged(self, git_repo: Path) -> None:
        (git_repo / "pyproject.toml").write_text('[tool.coverage.run]\nsource = ["src"]\n', encoding="utf-8")
        (git_repo / "scripts").mkdir()
        (git_repo / "scripts" / "tool.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
        diff = _worktree_diff(git_repo)
        data_file = _coverage_db_without(git_repo)
        report = measure_diff_coverage(diff, coverage_data_file=data_file, repo_root=git_repo)
        # scripts/ is outside source=["src"] — not a coverage finding,
        # and its symbol is not flagged either.
        assert report.passes(), (report.uncovered, report.unreferenced_symbols)

    def test_explicit_scope_argument_overrides_pyproject(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text("def widget():\n    return 7\n", encoding="utf-8")
        diff = _worktree_diff(git_repo, "shipped.py")
        data_file = _coverage_db_without(git_repo)
        scope = CoverageScope(source_roots=("src",), omit=())
        report = measure_diff_coverage(diff, coverage_data_file=data_file, repo_root=git_repo, scope=scope)
        assert report.passes()
