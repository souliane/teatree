"""Golden corpus + behaviour tests for the conservative test-shape check.

The corpus is the load-bearing part: a **must-FLAG** set (copy-pasted test
methods that should be one ``@pytest.mark.parametrize``) AND a symmetric,
non-negotiable **must-NOT-FLAG** set (a parametrized test, a justified
edge-case unit test, legitimately distinct tests). The must-NOT-FLAG set is
what proves the check cannot false-positive on legitimate shapes — a gate
without it is incomplete (gate over-block doctrine).

Fixtures live as ``*.py.txt`` so pytest never collects them as test modules and
they never pollute the live test:source ratio.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.quality.test_shape import (
    Baseline,
    DupConfig,
    Mode,
    RatioMeasurement,
    RatioRegression,
    TestShapeConfig,
    autouse_fixture_names,
    build_report,
    collect_source_files,
    collect_test_files,
    detect_ratio_regression,
    find_duplicate_clusters,
    find_shadowed_autouse_fixtures,
    load_config,
    loosens_baseline,
    measure_ratio,
)

_AUTOUSE_FIXTURE = "import pytest\n@pytest.fixture(autouse=True)\ndef clear_overlay_cache():\n    reset()\n    yield\n"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _suppress_update_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.cli._maybe_show_update_notice", lambda: None)


_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "test_shape"
_MUST_FLAG = sorted((_FIXTURES / "must_flag").glob("*.py.txt"))
_MUST_NOT_FLAG = sorted((_FIXTURES / "must_not_flag").glob("*.py.txt"))


class TestGoldenCorpus:
    def test_corpus_has_both_dimensions(self) -> None:
        assert _MUST_FLAG, "must-FLAG corpus is empty"
        assert _MUST_NOT_FLAG, "must-NOT-FLAG corpus is empty (over-block dimension missing)"

    @pytest.mark.parametrize("fixture", _MUST_FLAG, ids=[p.stem for p in _MUST_FLAG])
    def test_must_flag_yields_a_cluster(self, fixture: Path) -> None:
        clusters = find_duplicate_clusters(fixture.read_text(encoding="utf-8"), fixture.name, DupConfig())
        assert clusters, f"{fixture.name}: expected a near-duplicate cluster, got none"

    @pytest.mark.parametrize("fixture", _MUST_NOT_FLAG, ids=[p.stem for p in _MUST_NOT_FLAG])
    def test_must_not_flag_yields_no_cluster(self, fixture: Path) -> None:
        clusters = find_duplicate_clusters(fixture.read_text(encoding="utf-8"), fixture.name, DupConfig())
        assert not clusters, f"{fixture.name}: false-positive cluster {[c.functions for c in clusters]}"


class TestDuplicateDetection:
    def test_below_min_cluster_is_not_flagged(self) -> None:
        source = (_FIXTURES / "must_flag" / "five_copy_pasted_methods.py.txt").read_text(encoding="utf-8")
        assert not find_duplicate_clusters(source, "x", DupConfig(min_cluster=6))

    def test_syntax_error_yields_no_cluster(self) -> None:
        assert find_duplicate_clusters("def test_(:\n", "broken.py") == []

    def test_non_test_functions_ignored(self) -> None:
        source = (
            "def helper_a():\n    return parse('a')\n"
            "def helper_b():\n    return parse('b')\n"
            "def helper_c():\n    return parse('c')\n"
        )
        assert not find_duplicate_clusters(source, "x", DupConfig(min_cluster=2))


class TestRatioRegression:
    @pytest.mark.parametrize(
        ("measured_test", "measured_source", "expect_regression"),
        [
            (50, 100, True),
            (199, 100, False),
            (205, 100, False),
            (196, 100, False),
        ],
        ids=["ratio_halved", "at_baseline", "above_baseline", "within_tolerance"],
    )
    def test_regression_only_on_drop_past_tolerance(
        self, measured_test: int, measured_source: int, *, expect_regression: bool
    ) -> None:
        baseline = Baseline(test_lines=200, source_lines=100, tolerance=0.05)
        measured = RatioMeasurement(test_lines=measured_test, source_lines=measured_source)
        regression = detect_ratio_regression(measured, baseline)
        assert (regression is not None) is expect_regression

    def test_empty_baseline_never_regresses(self) -> None:
        baseline = Baseline(test_lines=0, source_lines=0)
        assert detect_ratio_regression(RatioMeasurement(0, 100), baseline) is None

    @pytest.mark.parametrize(
        ("measured_test", "measured_source", "expect_loosens"),
        [
            (150, 100, True),
            (200, 100, False),
            (250, 100, False),
            (199, 100, True),
        ],
        ids=["worse_ratio", "same_ratio", "better_ratio", "one_line_worse"],
    )
    def test_loosens_only_when_strictly_below_committed(
        self, measured_test: int, measured_source: int, *, expect_loosens: bool
    ) -> None:
        baseline = Baseline(test_lines=200, source_lines=100, tolerance=0.05)
        measured = RatioMeasurement(test_lines=measured_test, source_lines=measured_source)
        assert loosens_baseline(measured, baseline) is expect_loosens

    def test_first_ever_baseline_loosens_nothing(self) -> None:
        empty = Baseline(test_lines=0, source_lines=0)
        assert loosens_baseline(RatioMeasurement(1, 100), empty) is False

    def test_measure_ratio_counts_significant_lines(self, tmp_path: Path) -> None:
        test_file = tmp_path / "t.py"
        test_file.write_text("# comment\n\ndef test_a():\n    assert True\n", encoding="utf-8")
        source_file = tmp_path / "s.py"
        source_file.write_text("x = 1\ny = 2\n", encoding="utf-8")
        measured = measure_ratio(test_files=[test_file], source_files=[source_file])
        assert measured.test_lines == 2
        assert measured.source_lines == 2


class TestConfigLoading:
    def test_defaults_to_warn_when_absent(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "pyproject.toml")
        assert config.mode is Mode.WARN
        assert config.baseline is None

    def test_reads_mode_and_baseline(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[tool.teatree.test_shape]\nmode = "block"\nmin_cluster = 4\n'
            "test_lines = 300\nsource_lines = 100\ntolerance = 0.02\n",
            encoding="utf-8",
        )
        config = load_config(pyproject)
        assert config.mode is Mode.BLOCK
        assert config.dup.min_cluster == 4
        assert config.baseline == Baseline(test_lines=300, source_lines=100, tolerance=0.02)

    def test_invalid_mode_rejected(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[tool.teatree.test_shape]\nmode = "nope"\n', encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid test_shape mode"):
            load_config(pyproject)


def _flaggable_tree(tmp_path: Path) -> tuple[Path, Path]:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_x.py"
    test_file.write_text(
        (_FIXTURES / "must_flag" / "five_copy_pasted_methods.py.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    src_dir = tmp_path / "src" / "teatree"
    src_dir.mkdir(parents=True)
    source_file = src_dir / "mod.py"
    source_file.write_text("x = 1\n", encoding="utf-8")
    return test_file, source_file


class TestReportModeSemantics:
    def test_warn_mode_reports_but_never_blocks(self, tmp_path: Path) -> None:
        test_file, source_file = _flaggable_tree(tmp_path)
        report = build_report(
            test_files=[test_file],
            source_files=[source_file],
            config=TestShapeConfig(mode=Mode.WARN),
        )
        assert report.has_findings
        assert not report.should_block

    def test_block_mode_blocks_on_findings(self, tmp_path: Path) -> None:
        test_file, source_file = _flaggable_tree(tmp_path)
        report = build_report(
            test_files=[test_file],
            source_files=[source_file],
            config=TestShapeConfig(mode=Mode.BLOCK),
        )
        assert report.should_block


def _make_repo(tmp_path: Path, *, mode: str, flaggable: bool) -> Path:
    (tmp_path / "tests").mkdir()
    body = (
        (_FIXTURES / "must_flag" / "five_copy_pasted_methods.py.txt").read_text(encoding="utf-8")
        if flaggable
        else "def test_only_one():\n    assert True\n"
    )
    (tmp_path / "tests" / "test_x.py").write_text(body, encoding="utf-8")
    (tmp_path / "src" / "teatree").mkdir(parents=True)
    (tmp_path / "src" / "teatree" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.teatree.test_shape]\nmode = "{mode}"\nmin_cluster = 3\n', encoding="utf-8"
    )
    return tmp_path


class TestCli:
    def test_block_mode_exits_nonzero_on_findings(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, mode="block", flaggable=True)
        result = runner.invoke(app, ["tool", "test-shape", "--root", str(repo)])
        assert result.exit_code == 1
        assert "BLOCK" in result.output

    def test_warn_mode_exits_zero_even_with_findings(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, mode="warn", flaggable=True)
        result = runner.invoke(app, ["tool", "test-shape", "--root", str(repo)])
        assert result.exit_code == 0
        assert "advisory" in result.output.lower()

    def test_clean_repo_reports_no_findings(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, mode="block", flaggable=False)
        result = runner.invoke(app, ["tool", "test-shape", "--root", str(repo)])
        assert result.exit_code == 0
        assert "no findings" in result.output.lower()

    def test_json_output_is_machine_readable(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, mode="warn", flaggable=True)
        result = runner.invoke(app, ["tool", "test-shape", "--root", str(repo), "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["mode"] == "warn"
        assert payload["has_findings"] is True
        assert payload["should_block"] is False

    def test_update_baseline_rewrites_pyproject_counts(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, mode="warn", flaggable=True)
        result = runner.invoke(app, ["tool", "test-shape", "--root", str(repo), "--update-baseline"])
        assert result.exit_code == 0
        config = load_config(repo / "pyproject.toml")
        assert config.baseline is not None
        assert config.baseline.source_lines == 1
        assert config.mode is Mode.WARN


def _repo_with_committed_baseline(tmp_path: Path, *, test_lines: int, source_lines: int) -> Path:
    """A repo with a fixed live ratio and a configurable committed baseline.

    The test module has 2 significant lines and the source module 1, so the LIVE
    ratio is 2.0. A committed baseline above 2.0 makes a `--update-baseline` a
    loosening (the live ratio is worse than committed); below 2.0 makes it a
    tightening.
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_one():\n    assert True\n", encoding="utf-8")
    (tmp_path / "src" / "teatree").mkdir(parents=True)
    (tmp_path / "src" / "teatree" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.teatree.test_shape]\nmode = "warn"\nmin_cluster = 3\n'
        f"test_lines = {test_lines}\nsource_lines = {source_lines}\n",
        encoding="utf-8",
    )
    return tmp_path


class TestUpdateBaselineRatchet:
    def test_refuses_to_loosen_a_committed_baseline(self, tmp_path: Path) -> None:
        repo = _repo_with_committed_baseline(tmp_path, test_lines=5, source_lines=1)
        result = runner.invoke(app, ["tool", "test-shape", "--root", str(repo), "--update-baseline"])
        assert result.exit_code == 1
        assert "refusing to loosen" in result.output.lower()
        committed = load_config(repo / "pyproject.toml").baseline
        assert committed == Baseline(test_lines=5, source_lines=1)

    def test_allow_regression_overrides_the_refusal(self, tmp_path: Path) -> None:
        repo = _repo_with_committed_baseline(tmp_path, test_lines=5, source_lines=1)
        result = runner.invoke(
            app, ["tool", "test-shape", "--root", str(repo), "--update-baseline", "--allow-regression"]
        )
        assert result.exit_code == 0
        assert "loosened" in result.output.lower()
        committed = load_config(repo / "pyproject.toml").baseline
        assert committed == Baseline(test_lines=2, source_lines=1)

    def test_tightening_update_always_allowed(self, tmp_path: Path) -> None:
        repo = _repo_with_committed_baseline(tmp_path, test_lines=1, source_lines=5)
        result = runner.invoke(app, ["tool", "test-shape", "--root", str(repo), "--update-baseline"])
        assert result.exit_code == 0
        assert "ratcheted" in result.output.lower()
        committed = load_config(repo / "pyproject.toml").baseline
        assert committed == Baseline(test_lines=2, source_lines=1)

    def test_first_ever_baseline_writes_without_a_committed_value(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, mode="warn", flaggable=False)
        assert load_config(repo / "pyproject.toml").baseline is None
        result = runner.invoke(app, ["tool", "test-shape", "--root", str(repo), "--update-baseline"])
        assert result.exit_code == 0
        assert load_config(repo / "pyproject.toml").baseline is not None


class TestRobustness:
    def test_missing_file_reads_as_empty(self, tmp_path: Path) -> None:
        missing = tmp_path / "gone.py"
        measured = measure_ratio(test_files=[missing], source_files=[missing])
        assert measured.test_lines == 0
        assert measured.source_lines == 0

    def test_collectors_return_empty_without_dirs(self, tmp_path: Path) -> None:
        assert collect_test_files(tmp_path) == []
        assert collect_source_files(tmp_path) == []

    def test_collectors_find_files(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("def test_a(): ...\n", encoding="utf-8")
        (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (tmp_path / "src" / "teatree").mkdir(parents=True)
        (tmp_path / "src" / "teatree" / "mod.py").write_text("x = 1\n", encoding="utf-8")
        test_files = collect_test_files(tmp_path)
        assert [p.name for p in test_files] == ["test_a.py"]
        assert [p.name for p in collect_source_files(tmp_path)] == ["mod.py"]

    def test_distinct_arg_shapes_do_not_collapse(self) -> None:
        source = (
            "def test_no_args():\n    assert run() == 1\n"
            "def test_one_arg(a):\n    assert run(a) == 1\n"
            "def test_two_args(a, b):\n    assert run(a, b) == 1\n"
        )
        assert not find_duplicate_clusters(source, "x", DupConfig(min_cluster=2))

    def test_nested_function_args_are_normalized(self) -> None:
        body = "    def inner(payload):\n        return payload\n    assert inner(1) == 1\n"
        source = "".join(f"def test_n{i}():\n{body}" for i in range(3))
        clusters = find_duplicate_clusters(source, "x", DupConfig(min_cluster=3))
        assert len(clusters) == 1

    def test_multiple_decorators_including_plain_name_are_scanned(self) -> None:
        source = (
            "@mark\n@parametrize('v', [1, 2])\ndef test_a(v):\n    assert v\n"
            "@mark\n@parametrize('v', [3, 4])\ndef test_b(v):\n    assert v\n"
            "@mark\n@parametrize('v', [5, 6])\ndef test_c(v):\n    assert v\n"
        )
        assert not find_duplicate_clusters(source, "x", DupConfig(min_cluster=2))

    def test_unusual_decorator_shape_is_skipped_not_crashed(self) -> None:
        source = (
            "@registry['mark']\ndef test_a():\n    assert run() == 1\n"
            "@registry['mark']\ndef test_b():\n    assert run() == 1\n"
            "@registry['mark']\ndef test_c():\n    assert run() == 1\n"
        )
        clusters = find_duplicate_clusters(source, "x", DupConfig(min_cluster=3))
        assert len(clusters) == 1

    def test_ratio_regression_message_names_baseline_and_measured(self) -> None:
        regression = RatioRegression(
            measured=RatioMeasurement(test_lines=10, source_lines=100),
            baseline=Baseline(test_lines=200, source_lines=100, tolerance=0.05),
        )
        message = regression.message
        assert "regressed" in message
        assert "2.000" in message

    def test_build_report_with_baseline_flags_ratio_regression(self, tmp_path: Path) -> None:
        test_file = tmp_path / "test_x.py"
        test_file.write_text("def test_one():\n    assert True\n", encoding="utf-8")
        source_file = tmp_path / "mod.py"
        source_file.write_text("\n".join(f"x{i} = {i}" for i in range(50)) + "\n", encoding="utf-8")
        config = TestShapeConfig(mode=Mode.WARN, baseline=Baseline(test_lines=200, source_lines=100, tolerance=0.05))
        report = build_report(test_files=[test_file], source_files=[source_file], config=config)
        assert report.ratio_regression is not None
        assert any("regressed" in line for line in report.summary_lines())


class TestAutouseFixtureNames:
    def test_extracts_autouse_fixture_name(self) -> None:
        assert autouse_fixture_names(_AUTOUSE_FIXTURE) == {"clear_overlay_cache"}

    def test_ignores_non_autouse_fixture(self) -> None:
        source = "import pytest\n@pytest.fixture\ndef thing():\n    yield\n"
        assert autouse_fixture_names(source) == set()

    def test_ignores_explicit_autouse_false(self) -> None:
        source = "import pytest\n@pytest.fixture(autouse=False)\ndef thing():\n    yield\n"
        assert autouse_fixture_names(source) == set()

    def test_syntax_error_yields_no_names(self) -> None:
        assert autouse_fixture_names("def broken(:\n") == set()


class TestShadowedAutouseFixtures:
    def _make_tree(self, root: Path) -> None:
        (root / "conftest.py").write_text(_AUTOUSE_FIXTURE, encoding="utf-8")

    def test_flags_test_file_shadowing_ancestor_conftest(self, tmp_path: Path) -> None:
        self._make_tree(tmp_path)
        shadow = tmp_path / "test_thing.py"
        shadow.write_text(_AUTOUSE_FIXTURE, encoding="utf-8")
        findings = find_shadowed_autouse_fixtures(test_files=[shadow], root=tmp_path)
        assert len(findings) == 1
        assert findings[0].name == "clear_overlay_cache"
        assert findings[0].ancestor_conftest == str(tmp_path / "conftest.py")

    def test_flags_deeper_conftest_shadowing_ancestor(self, tmp_path: Path) -> None:
        self._make_tree(tmp_path)
        sub = tmp_path / "sub"
        sub.mkdir()
        deeper = sub / "conftest.py"
        deeper.write_text(_AUTOUSE_FIXTURE, encoding="utf-8")
        findings = find_shadowed_autouse_fixtures(test_files=[deeper], root=tmp_path)
        assert [f.name for f in findings] == ["clear_overlay_cache"]

    def test_does_not_flag_unique_fixture_name(self, tmp_path: Path) -> None:
        self._make_tree(tmp_path)
        other = tmp_path / "test_other.py"
        other.write_text(
            "import pytest\n@pytest.fixture(autouse=True)\ndef distinct_cache():\n    yield\n",
            encoding="utf-8",
        )
        assert find_shadowed_autouse_fixtures(test_files=[other], root=tmp_path) == []

    def test_does_not_flag_when_no_ancestor_defines_it(self, tmp_path: Path) -> None:
        lone = tmp_path / "test_lone.py"
        lone.write_text(_AUTOUSE_FIXTURE, encoding="utf-8")
        assert find_shadowed_autouse_fixtures(test_files=[lone], root=tmp_path) == []

    def test_build_report_surfaces_shadowed_fixture(self, tmp_path: Path) -> None:
        self._make_tree(tmp_path)
        shadow = tmp_path / "test_thing.py"
        shadow.write_text(_AUTOUSE_FIXTURE, encoding="utf-8")
        report = build_report(
            test_files=[shadow],
            source_files=[],
            config=TestShapeConfig(mode=Mode.WARN),
            root=tmp_path,
        )
        assert report.has_findings
        assert any("shadows" in line for line in report.summary_lines())

    def test_build_report_without_root_skips_shadow_check(self, tmp_path: Path) -> None:
        self._make_tree(tmp_path)
        shadow = tmp_path / "test_thing.py"
        shadow.write_text(_AUTOUSE_FIXTURE, encoding="utf-8")
        report = build_report(
            test_files=[shadow],
            source_files=[],
            config=TestShapeConfig(mode=Mode.WARN),
        )
        assert report.shadowed_fixtures == ()
