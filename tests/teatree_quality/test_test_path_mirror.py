"""Fitness function: every test file mirrors its ``src/teatree/<pkg>/...`` path.

The forward-guard for the repo bar *tests mirror production code* (``CLAUDE.md``
+ ``/ac-python``). ~205 existing files predate the convention; this gate freezes
that floor (``[tool.teatree.test_path_mirror] baseline``) so the relocation sweep
can only ever shrink the live mis-pathed count, never grow it.

Three halves:

:class:`TestLiveTree` is the gate itself — the live violation count never exceeds
the committed baseline.

:class:`TestGoldenCorpus` proves the checker is neither vacuous nor over-blocking
against the committed ``*.py.txt`` corpus: a must-FLAG case (loose-at-root) and a
symmetric must-NOT-FLAG set (a correctly mirrored file, a cross-cutting-pragma
file). Each fixture's content is planted at the location its name describes so the
location-dependent verdict is exercised end to end.

:class:`TestRatchet` is the anti-vacuity proof: a synthetic tree at baseline+1
makes the verdict fire (reverting the count comparison to always-pass turns it
green), and ``--update-baseline`` refuses to record a HIGHER count without
``--allow-regression``.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.test_path_mirror_tools import _update_baseline
from teatree.quality.test_path_mirror import (
    MirrorConfig,
    MirrorReport,
    MirrorViolation,
    build_report,
    check_file,
    expected_test_dir,
    first_party_imports,
    is_exempt,
    load_config,
    loosens_baseline,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "test_path_mirror"
_MUST_FLAG = sorted((_FIXTURES / "must_flag").glob("*.py.txt"))
_MUST_NOT_FLAG = sorted((_FIXTURES / "must_not_flag").glob("*.py.txt"))

_FIXTURE_PLACEMENT: dict[str, str] = {
    "loose_at_root": "tests",
    "mirrored": "tests/teatree_hooks",
    "cross_cutting_pragma": "tests/test_anywhere_dir",
    "mispathed_package_with_toplevel_import": "tests/teatree_hooks",
}

runner = CliRunner()


def _seed_src_tree(root: Path) -> None:
    src = root / "src" / "teatree"
    for package in ("hooks", "core"):
        (src / package).mkdir(parents=True, exist_ok=True)
    (src / "identity.py").write_text("current_user = None\n", encoding="utf-8")


def _make_repo(root: Path, *, baseline: int, loose_files: int) -> Path:
    (root / "src" / "teatree").mkdir(parents=True)
    (root / "src" / "teatree" / "hooks").mkdir()
    for n in range(loose_files):
        _plant(root, "tests", f"test_loose_{n}.py", "from teatree.hooks.x import y\n")
    (root / "pyproject.toml").write_text(
        f'[tool.teatree.test_path_mirror]\nmode = "block"\nbaseline = {baseline}\n', encoding="utf-8"
    )
    return root


def _plant(root: Path, rel_dir: str, name: str, body: str) -> Path:
    directory = root / rel_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def _plant_fixture(root: Path, fixture: Path) -> Path:
    _seed_src_tree(root)
    stem = fixture.name.removesuffix(".py.txt")
    return _plant(root, _FIXTURE_PLACEMENT[stem], f"test_{stem}.py", fixture.read_text(encoding="utf-8"))


class TestLiveTree:
    def test_live_count_within_baseline(self) -> None:
        config = load_config(_REPO_ROOT / "pyproject.toml")
        report = build_report(root=_REPO_ROOT, config=config)
        assert not report.exceeds_baseline, (
            f"{report.live_count} mis-pathed test file(s) exceed baseline {report.baseline}:\n"
            + "\n".join(report.summary_lines())
        )


class TestMessages:
    def test_message_names_expected_dirs_when_present(self) -> None:
        violation = MirrorViolation(
            path="tests/test_x.py",
            imported_modules=("teatree.hooks.x",),
            expected_dirs=("tests/teatree_hooks",),
        )
        assert "tests/teatree_hooks" in violation.message
        assert "tests/test_x.py" in violation.message

    def test_message_falls_back_when_no_expected_dirs(self) -> None:
        violation = MirrorViolation(path="tests/test_x.py", imported_modules=("teatree.x",), expected_dirs=())
        assert "no first-party teatree import" in violation.message

    def test_summary_lines_one_per_violation(self) -> None:
        report = MirrorReport(
            violations=(
                MirrorViolation(
                    path="a.py", imported_modules=("teatree.hooks.x",), expected_dirs=("tests/teatree_hooks",)
                ),
                MirrorViolation(
                    path="b.py", imported_modules=("teatree.core.y",), expected_dirs=("tests/teatree_core",)
                ),
            ),
            baseline=0,
        )
        assert len(report.summary_lines()) == 2


class TestDegradation:
    def test_syntax_error_yields_no_imports(self) -> None:
        assert first_party_imports("def broken(:\n") == ()

    def test_unreadable_file_is_not_a_violation(self, tmp_path: Path) -> None:
        directory = tmp_path / "tests"
        directory.mkdir()
        missing = directory / "test_gone.py"
        assert check_file(missing, tmp_path) is None

    def test_missing_pyproject_yields_default_config(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "pyproject.toml")
        assert config.baseline == 0
        assert config.mode == "warn"


class TestExpectedDir:
    @pytest.mark.parametrize(
        ("module", "expected_path", "exact_only"),
        [
            ("teatree.hooks.banned_terms_scanner", "tests/teatree_hooks", False),
            ("teatree.quality.test_path_mirror", "tests/teatree_quality", False),
            ("teatree.backends.gitlab.ci", "tests/teatree_backends/gitlab", False),
            ("teatree.core.models.merge_clear", "tests/teatree_core/models", False),
            ("teatree.config", "tests/teatree_config", False),
            ("teatree.identity", "tests", True),
        ],
    )
    def test_module_maps_to_mirror_dir(self, module: str, expected_path: str, *, exact_only: bool) -> None:
        result = expected_test_dir(module, _REPO_ROOT)
        assert result is not None
        assert result.path == expected_path
        assert result.exact_only is exact_only

    def test_bare_root_module_maps_to_nothing(self) -> None:
        assert expected_test_dir("teatree", _REPO_ROOT) is None

    def test_toplevel_module_expectation_demands_exact_root(self) -> None:
        expectation = expected_test_dir("teatree.identity", _REPO_ROOT)
        assert expectation is not None
        assert expectation.satisfied_by("tests")
        assert not expectation.satisfied_by("tests/teatree_hooks")

    def test_package_expectation_allows_descendant(self) -> None:
        expectation = expected_test_dir("teatree.core.models.merge_clear", _REPO_ROOT)
        assert expectation is not None
        assert expectation.satisfied_by("tests/teatree_core/models")
        assert expectation.satisfied_by("tests/teatree_core/models/deeper")
        assert not expectation.satisfied_by("tests/teatree_hooks")


class TestImportExtraction:
    def test_extracts_from_and_plain_imports(self) -> None:
        source = "import teatree.core.models\nfrom teatree.cli import tools\nimport os\n"
        assert first_party_imports(source) == ("teatree.core.models", "teatree.cli")

    def test_ignores_relative_and_thirdparty(self) -> None:
        source = "from . import sibling\nfrom pytest import fixture\n"
        assert first_party_imports(source) == ()


class TestGoldenCorpus:
    def test_corpus_has_both_dimensions(self) -> None:
        assert _MUST_FLAG, "must-FLAG corpus is empty"
        assert _MUST_NOT_FLAG, "must-NOT-FLAG corpus is empty (over-block dimension missing)"

    @pytest.mark.parametrize("fixture", _MUST_FLAG, ids=[p.stem for p in _MUST_FLAG])
    def test_must_flag_fixture_is_violation(self, fixture: Path, tmp_path: Path) -> None:
        planted = _plant_fixture(tmp_path, fixture)
        assert check_file(planted, tmp_path) is not None, f"{fixture.name} should be flagged but was not"

    @pytest.mark.parametrize("fixture", _MUST_NOT_FLAG, ids=[p.stem for p in _MUST_NOT_FLAG])
    def test_must_not_flag_fixture_is_clean(self, fixture: Path, tmp_path: Path) -> None:
        planted = _plant_fixture(tmp_path, fixture)
        assert check_file(planted, tmp_path) is None, f"{fixture.name} wrongly flagged"


class TestToplevelImportLoophole:
    def test_mispathed_package_test_still_flagged_despite_toplevel_import(self, tmp_path: Path) -> None:
        _seed_src_tree(tmp_path)
        planted = _plant(
            tmp_path,
            "tests/teatree_hooks",
            "test_x.py",
            "from teatree.core.models import Ticket\nfrom teatree.identity import current_user\n",
        )
        assert check_file(planted, tmp_path) is not None

    def test_toplevel_import_alone_does_not_excuse_a_subdir(self, tmp_path: Path) -> None:
        _seed_src_tree(tmp_path)
        planted = _plant(tmp_path, "tests/teatree_hooks", "test_x.py", "from teatree.identity import current_user\n")
        assert check_file(planted, tmp_path) is not None

    def test_legit_helper_import_stays_clean(self, tmp_path: Path) -> None:
        _seed_src_tree(tmp_path)
        planted = _plant(
            tmp_path,
            "tests/teatree_core",
            "test_x.py",
            "from teatree.core.models import Ticket\nfrom teatree.identity import current_user\n",
        )
        assert check_file(planted, tmp_path) is None

    def test_toplevel_module_test_at_root_stays_clean(self, tmp_path: Path) -> None:
        _seed_src_tree(tmp_path)
        planted = _plant(tmp_path, "tests", "test_identity.py", "from teatree.identity import current_user\n")
        assert check_file(planted, tmp_path) is None


class TestExemptions:
    @pytest.mark.parametrize("name", ["conftest.py", "factories.py", "__init__.py"])
    def test_scaffolding_filename_exempt(self, name: str, tmp_path: Path) -> None:
        planted = _plant(tmp_path, "tests", name, "from teatree.hooks.x import y\n")
        assert check_file(planted, tmp_path) is None

    @pytest.mark.parametrize(
        "rel_dir",
        [
            "tests/integration",
            "tests/conformance",
            "tests/e2e_flows",
            "tests/fixtures",
            "tests/eval_replay",
            "tests/eval_harness",
        ],
    )
    def test_exempt_dir_prefix(self, rel_dir: str, tmp_path: Path) -> None:
        planted = _plant(tmp_path, rel_dir, "test_x.py", "from teatree.hooks.x import y\n")
        assert check_file(planted, tmp_path) is None

    @pytest.mark.parametrize("name", ["conftest.py", "factories.py", "__init__.py"])
    def test_is_exempt_filename(self, name: str, tmp_path: Path) -> None:
        planted = _plant(tmp_path, "tests", name, "from teatree.hooks.x import y\n")
        assert is_exempt(planted, tmp_path)


class TestRatchet:
    def test_fails_when_violations_exceed_baseline(self, tmp_path: Path) -> None:
        for n in range(3):
            _plant(tmp_path, "tests", f"test_loose_{n}.py", "from teatree.hooks.x import y\n")
        report = build_report(root=tmp_path, config=MirrorConfig(baseline=2))
        assert report.live_count == 3
        assert report.exceeds_baseline

    def test_holds_when_violations_at_baseline(self, tmp_path: Path) -> None:
        for n in range(2):
            _plant(tmp_path, "tests", f"test_loose_{n}.py", "from teatree.hooks.x import y\n")
        report = build_report(root=tmp_path, config=MirrorConfig(baseline=2))
        assert not report.exceeds_baseline

    def test_refuses_to_loosen_baseline(self) -> None:
        assert loosens_baseline(measured=10, baseline=5) is True

    def test_allows_tightening_baseline(self) -> None:
        assert loosens_baseline(measured=3, baseline=5) is False
        assert loosens_baseline(measured=5, baseline=5) is False


class TestCliUpdateBaseline:
    def test_update_refuses_higher_count_without_allow(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[tool.teatree.test_path_mirror]\nbaseline = 0\n", encoding="utf-8")
        _plant(tmp_path, "tests", "test_loose.py", "from teatree.hooks.x import y\n")
        with pytest.raises(typer.Exit) as exc:
            _update_baseline(pyproject, tmp_path, allow_regression=False)
        assert exc.value.exit_code == 1
        assert load_config(pyproject).baseline == 0

    def test_update_records_lower_count(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[tool.teatree.test_path_mirror]\nbaseline = 5\n", encoding="utf-8")
        _update_baseline(pyproject, tmp_path, allow_regression=False)
        assert load_config(pyproject).baseline == 0


class TestCli:
    def test_within_baseline_exits_zero(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, baseline=2, loose_files=1)
        result = runner.invoke(app, ["tool", "test-path-mirror", "--root", str(repo)])
        assert result.exit_code == 0
        assert "ratchet holds" in result.output

    def test_regression_exits_nonzero(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, baseline=0, loose_files=2)
        result = runner.invoke(app, ["tool", "test-path-mirror", "--root", str(repo)])
        assert result.exit_code == 1
        assert "REGRESSION" in result.output

    def test_json_stdout_is_pure_json_even_with_update_banner(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, baseline=0, loose_files=2)
        with patch("teatree.config.check_for_updates", return_value="teatree 9.9.9 available (you have 0.0.1)"):
            result = runner.invoke(app, ["tool", "test-path-mirror", "--root", str(repo), "--json"])
        payload = json.loads(result.stdout)
        assert payload["baseline"] == 0
        assert payload["live_count"] == 2
        assert payload["exceeds_baseline"] is True
        assert "[update]" not in result.stdout

    def test_update_baseline_rewrites_pyproject(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, baseline=5, loose_files=2)
        result = runner.invoke(app, ["tool", "test-path-mirror", "--root", str(repo), "--update-baseline"])
        assert result.exit_code == 0
        assert load_config(repo / "pyproject.toml").baseline == 2

    def test_update_baseline_refuses_rise_without_allow(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, baseline=0, loose_files=2)
        result = runner.invoke(app, ["tool", "test-path-mirror", "--root", str(repo), "--update-baseline"])
        assert result.exit_code == 1
        assert load_config(repo / "pyproject.toml").baseline == 0

    def test_update_baseline_allows_rise_with_flag(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, baseline=0, loose_files=2)
        result = runner.invoke(
            app, ["tool", "test-path-mirror", "--root", str(repo), "--update-baseline", "--allow-regression"]
        )
        assert result.exit_code == 0
        assert load_config(repo / "pyproject.toml").baseline == 2
