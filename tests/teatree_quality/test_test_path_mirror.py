"""Fitness function: every test file mirrors its ``src/teatree/<pkg>/...`` path.

The forward-guard for the repo bar *tests mirror production code* (``CLAUDE.md``
+ ``/ac-python``). ~191 existing files predate the convention; this gate
grandfathers that floor as an explicit per-path LEDGER
(``[tool.teatree.test_path_mirror] baseline_file``) so the relocation sweep can
only ever shrink the live mis-pathed set, never grow it — and, unlike a single
count baseline, two disjoint PRs never collide because a path list merges as a
git set-union.

The load-bearing halves:

:class:`TestLiveTree` is the gate itself — no live violation is un-grandfathered
and no ledger entry is stale.

:class:`TestUnknownViolation` / :class:`TestForcedBanking` are the anti-vacuity
proofs: a NEW mis-pathed file not in the ledger is RED (named), and a stale
ledger entry that no longer violates is RED (forced banking — the headroom hole
of the old count baseline is closed).

:class:`TestCollisionPin` is the concurrent-merge regression pin: the union of
two independently-valid grandfathered edits over the merged tree stays green —
the exact scenario a single scalar baseline went red on.

:class:`TestGoldenCorpus` proves the checker is neither vacuous nor over-blocking
against the committed ``*.py.txt`` corpus.
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
    Ledger,
    MirrorConfig,
    MirrorReport,
    MirrorViolation,
    build_report,
    check_file,
    expected_test_dir,
    first_party_imports,
    is_exempt,
    load_config,
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


def _loose_paths(n: int) -> list[str]:
    return [f"tests/test_loose_{i}.py" for i in range(n)]


def _make_repo(root: Path, *, grandfathered: list[str], loose_files: int) -> Path:
    (root / "src" / "teatree" / "hooks").mkdir(parents=True)
    for n in range(loose_files):
        _plant(root, "tests", f"test_loose_{n}.py", "from teatree.hooks.x import y\n")
    (root / "pyproject.toml").write_text(
        '[tool.teatree.test_path_mirror]\nmode = "block"\nbaseline_file = "grandfathered.txt"\n', encoding="utf-8"
    )
    Ledger.write(root / "grandfathered.txt", grandfathered)
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
    def test_live_tree_ledger_is_exact(self) -> None:
        config = load_config(_REPO_ROOT / "pyproject.toml")
        report = build_report(root=_REPO_ROOT, config=config)
        assert not report.failed, (
            f"{len(report.unknown_violations)} new mis-pathed file(s):\n"
            + "\n".join(report.summary_lines())
            + f"\n{len(report.stale_entries)} stale ledger entry(ies):\n"
            + "\n".join(report.stale_lines())
        )

    def test_committed_ledger_matches_the_live_violation_set(self) -> None:
        # The cutover contract: the committed grandfathered set is EXACTLY the
        # current live violation set (no headroom, no stale entries).
        config = load_config(_REPO_ROOT / "pyproject.toml")
        report = build_report(root=_REPO_ROOT, config=config)
        assert config.grandfathered == report.live_paths


class TestLedgerConfig:
    def test_baseline_file_resolves_relative_to_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.teatree.test_path_mirror]\nbaseline_file = "sub/ledger.txt"\n', encoding="utf-8"
        )
        resolved = Ledger.path_for(tmp_path / "pyproject.toml")
        assert resolved == tmp_path / "sub" / "ledger.txt"

    def test_missing_baseline_file_yields_empty_set(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "pyproject.toml")
        assert config.grandfathered == frozenset()

    def test_ledger_round_trips_ignoring_comments_and_blanks(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.txt"
        Ledger.write(ledger, ["tests/b.py", "tests/a.py"])
        # The header (`#` lines) and a blank line are ignored on read.
        (tmp_path / "extra.txt").write_text("# a comment\n\ntests/c.py\n  tests/d.py  \n", encoding="utf-8")
        assert Ledger.load(ledger) == frozenset({"tests/a.py", "tests/b.py"})
        assert Ledger.load(tmp_path / "extra.txt") == frozenset({"tests/c.py", "tests/d.py"})

    def test_write_grandfathered_is_sorted_and_deterministic(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.txt"
        Ledger.write(ledger, ["tests/z.py", "tests/a.py", "tests/a.py"])
        body = [line for line in ledger.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]
        assert body == ["tests/a.py", "tests/z.py"]


class TestUnknownViolation:
    def test_new_mispathed_file_not_in_ledger_is_red_and_named(self, tmp_path: Path) -> None:
        _seed_src_tree(tmp_path)
        planted = _plant(tmp_path, "tests", "test_new.py", "from teatree.hooks.x import y\n")
        rel = planted.relative_to(tmp_path).as_posix()
        report = build_report(root=tmp_path, config=MirrorConfig(grandfathered=frozenset()))
        assert report.failed
        assert [v.path for v in report.unknown_violations] == [rel]
        assert rel in "\n".join(report.summary_lines())

    def test_grandfathered_file_is_not_an_unknown_violation(self, tmp_path: Path) -> None:
        _seed_src_tree(tmp_path)
        planted = _plant(tmp_path, "tests", "test_old.py", "from teatree.hooks.x import y\n")
        rel = planted.relative_to(tmp_path).as_posix()
        report = build_report(root=tmp_path, config=MirrorConfig(grandfathered=frozenset({rel})))
        assert not report.failed


class TestForcedBanking:
    def test_deleted_grandfathered_path_is_stale_and_red(self, tmp_path: Path) -> None:
        _seed_src_tree(tmp_path)
        report = build_report(root=tmp_path, config=MirrorConfig(grandfathered=frozenset({"tests/test_gone.py"})))
        assert report.failed
        assert report.stale_entries == ("tests/test_gone.py",)
        assert "tests/test_gone.py" in "\n".join(report.stale_lines())

    def test_now_mirrored_grandfathered_path_is_stale_and_red(self, tmp_path: Path) -> None:
        # A file that was relocated to mirror correctly is no longer a violation;
        # its stale ledger entry must be banked (removed) or the gate stays red.
        _seed_src_tree(tmp_path)
        planted = _plant(tmp_path, "tests/teatree_hooks", "test_ok.py", "from teatree.hooks.x import y\n")
        rel = planted.relative_to(tmp_path).as_posix()
        report = build_report(root=tmp_path, config=MirrorConfig(grandfathered=frozenset({rel})))
        assert report.failed
        assert report.stale_entries == (rel,)


class TestCollisionPin:
    def test_disjoint_grandfathered_edits_union_stays_green(self, tmp_path: Path) -> None:
        # Merged tree of two disjoint PRs, each adding one mis-pathed file.
        _seed_src_tree(tmp_path)
        a = _plant(tmp_path, "tests", "test_a.py", "from teatree.hooks.x import y\n")
        b = _plant(tmp_path, "tests", "test_b.py", "from teatree.hooks.x import y\n")
        rel_a, rel_b = a.relative_to(tmp_path).as_posix(), b.relative_to(tmp_path).as_posix()
        # git unions the two independent line-additions -> the merged ledger holds
        # BOTH. This is the concurrent-merge scenario the scalar baseline went red
        # on (two +1s cannot union into +2); the per-path ledger stays green.
        union = build_report(root=tmp_path, config=MirrorConfig(grandfathered=frozenset({rel_a, rel_b})))
        assert not union.failed
        # Non-vacuity: with only ONE PR's ledger, the merged tree is RED — the
        # other file is an un-grandfathered violation. So the green above is the
        # UNION doing the work, not a vacuous pass.
        partial = build_report(root=tmp_path, config=MirrorConfig(grandfathered=frozenset({rel_a})))
        assert partial.failed
        assert [v.path for v in partial.unknown_violations] == [rel_b]


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

    def test_summary_lines_only_covers_unknown_violations(self) -> None:
        report = MirrorReport(
            violations=(
                MirrorViolation(
                    path="a.py", imported_modules=("teatree.hooks.x",), expected_dirs=("tests/teatree_hooks",)
                ),
                MirrorViolation(
                    path="b.py", imported_modules=("teatree.core.y",), expected_dirs=("tests/teatree_core",)
                ),
            ),
            grandfathered=frozenset({"a.py"}),
        )
        assert len(report.summary_lines()) == 1
        assert "b.py" in report.summary_lines()[0]


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
        assert config.grandfathered == frozenset()
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


class TestCliUpdateBaseline:
    def test_update_refuses_added_entry_without_allow(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, grandfathered=[], loose_files=1)
        with pytest.raises(typer.Exit) as exc:
            _update_baseline(repo / "pyproject.toml", repo, allow_regression=False)
        assert exc.value.exit_code == 1
        assert load_config(repo / "pyproject.toml").grandfathered == frozenset()

    def test_update_allows_added_entry_with_flag(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, grandfathered=[], loose_files=1)
        _update_baseline(repo / "pyproject.toml", repo, allow_regression=True)
        assert load_config(repo / "pyproject.toml").grandfathered == frozenset(_loose_paths(1))

    def test_update_banks_stale_entry(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, grandfathered=["tests/test_gone.py"], loose_files=0)
        _update_baseline(repo / "pyproject.toml", repo, allow_regression=False)
        assert load_config(repo / "pyproject.toml").grandfathered == frozenset()


class TestCli:
    def test_exact_ledger_exits_zero(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, grandfathered=_loose_paths(1), loose_files=1)
        result = runner.invoke(app, ["tool", "test-path-mirror", "--root", str(repo)])
        assert result.exit_code == 0
        assert "ratchet holds" in result.output

    def test_unknown_violation_exits_nonzero(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, grandfathered=[], loose_files=2)
        result = runner.invoke(app, ["tool", "test-path-mirror", "--root", str(repo)])
        assert result.exit_code == 1
        assert "REGRESSION" in result.output

    def test_stale_entry_exits_nonzero(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, grandfathered=["tests/test_gone.py"], loose_files=0)
        result = runner.invoke(app, ["tool", "test-path-mirror", "--root", str(repo)])
        assert result.exit_code == 1
        assert "STALE LEDGER" in result.output

    def test_json_stdout_is_pure_json_even_with_update_banner(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, grandfathered=[], loose_files=2)
        with patch("teatree.config.check_for_updates", return_value="teatree 9.9.9 available (you have 0.0.1)"):
            result = runner.invoke(app, ["tool", "test-path-mirror", "--root", str(repo), "--json"])
        payload = json.loads(result.stdout)
        assert payload["grandfathered_count"] == 0
        assert payload["live_count"] == 2
        assert payload["failed"] is True
        assert len(payload["unknown_violations"]) == 2
        assert "[update]" not in result.stdout

    def test_update_baseline_rewrites_ledger(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, grandfathered=["tests/test_gone.py"], loose_files=0)
        result = runner.invoke(app, ["tool", "test-path-mirror", "--root", str(repo), "--update-baseline"])
        assert result.exit_code == 0
        assert load_config(repo / "pyproject.toml").grandfathered == frozenset()

    def test_update_baseline_refuses_rise_without_allow(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, grandfathered=[], loose_files=2)
        result = runner.invoke(app, ["tool", "test-path-mirror", "--root", str(repo), "--update-baseline"])
        assert result.exit_code == 1
        assert load_config(repo / "pyproject.toml").grandfathered == frozenset()

    def test_update_baseline_allows_rise_with_flag(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, grandfathered=[], loose_files=2)
        result = runner.invoke(
            app, ["tool", "test-path-mirror", "--root", str(repo), "--update-baseline", "--allow-regression"]
        )
        assert result.exit_code == 0
        assert load_config(repo / "pyproject.toml").grandfathered == frozenset(_loose_paths(2))
