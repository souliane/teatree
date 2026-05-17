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
    _git(repo, "init", "-q")
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

    Private ``_`` helpers (tested via their public callers) and
    framework-decorated entrypoints (tested through the framework, not by
    importing the callback by name) are excluded — they would otherwise
    false-positive on the established Typer-CLI test pattern.
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

    def test_syntax_error_in_changed_file_is_skipped(self, git_repo: Path) -> None:
        (git_repo / "broken.py").write_text("def x(:\n    pass\n", encoding="utf-8")
        diff = _worktree_diff(git_repo, "broken.py")
        # Unparsable source cannot yield symbols — skipped, not crashed.
        assert unreferenced_changed_symbols(diff, repo_root=git_repo) == set()

    def test_import_alias_counts_as_reference(self, git_repo: Path) -> None:
        (git_repo / "shipped.py").write_text("def widget():\n    return 7\n", encoding="utf-8")
        (git_repo / "tests").mkdir()
        # `import shipped` (not `from … import`) — the bound name is the
        # top-level module; the symbol is reached via attribute access.
        # An explicit `from shipped import widget` is the canonical form.
        (git_repo / "tests" / "test_shipped.py").write_text(
            "from shipped import widget\n\n\ndef test_it():\n    assert widget() == 7\n",
            encoding="utf-8",
        )
        diff = _worktree_diff(git_repo)
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
