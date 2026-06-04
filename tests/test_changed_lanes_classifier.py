"""Tests for the diff-scoped CI lane classifier (souliane/teatree#132).

The classifier reads a list of changed paths and decides which CI lanes
must run. The headline property is FAIL-SAFE-UNKNOWN: any path the
classifier does not recognise forces ``all`` (run everything). The only
sanctioned skip is a provably pure-docs/markdown diff, which may skip the
HEAVY python lanes (``test``, ``mutation-diff``) while still running every
docs/markdown gate and every always-on security/quality lane.
"""

import io
import json

import pytest

from scripts.ci.changed_lanes import DOCS_LANES, HEAVY_PYTHON_LANES, PYTHON_LANES, Lanes, classify, main

_PY_PATHS = [
    "src/teatree/core/models/ticket.py",
    "tests/test_changed_lanes_classifier.py",
    "scripts/ci/changed_lanes.py",
    "src/teatree/cli/__init__.py",
]

_PURE_DOCS_PATHS = [
    "README.md",
    "BLUEPRINT.md",
    "docs/blueprint/configuration.md",
    "docs/generated/cli-reference.md",
    "CHANGELOG.md",
]

_CONFIG_PATHS = [
    "pyproject.toml",
    ".github/workflows/ci.yml",
    ".pre-commit-config.yaml",
    ".semgrep/blocking/foo.yml",
    "uv.lock",
    "Dockerfile",
    "dev/Dockerfile.test",
]


class TestFailSafeUnknown:
    @pytest.mark.parametrize(
        "path",
        [
            "some/unrecognized/file.xyz",
            "weird_extensionless_file",
            "data/blob.bin",
            "src/teatree/assets/logo.png",
            ".env.example",
            "Makefile",
        ],
    )
    def test_unknown_path_forces_all(self, path: str) -> None:
        lanes = classify([path])
        assert lanes.all is True, f"unrecognized path {path!r} must fail safe to all=True"

    def test_unknown_path_runs_every_lane(self) -> None:
        lanes = classify(["mystery.unknownext"])
        assert lanes.run_heavy_python is True
        assert lanes.run_python is True
        assert lanes.run_docs is True
        assert lanes.run_security is True

    def test_one_unknown_among_known_still_forces_all(self) -> None:
        lanes = classify(["README.md", "src/teatree/core/x.py", "mystery.qqq"])
        assert lanes.all is True

    def test_empty_diff_fails_safe_to_all(self) -> None:
        lanes = classify([])
        assert lanes.all is True


class TestPureDocsSkip:
    def test_pure_docs_skips_heavy_python(self) -> None:
        lanes = classify(_PURE_DOCS_PATHS)
        assert lanes.all is False
        assert lanes.run_heavy_python is False
        assert lanes.run_python is False

    def test_pure_docs_still_runs_docs_lanes(self) -> None:
        lanes = classify(_PURE_DOCS_PATHS)
        assert lanes.run_docs is True

    def test_pure_docs_still_runs_security_lanes(self) -> None:
        lanes = classify(_PURE_DOCS_PATHS)
        assert lanes.run_security is True

    @pytest.mark.parametrize("path", _PURE_DOCS_PATHS)
    def test_each_docs_path_alone_skips_heavy(self, path: str) -> None:
        lanes = classify([path])
        assert lanes.run_heavy_python is False
        assert lanes.run_docs is True
        assert lanes.run_security is True


class TestPythonForcesAllPythonLanes:
    @pytest.mark.parametrize("path", _PY_PATHS)
    def test_py_change_runs_all_python_lanes(self, path: str) -> None:
        lanes = classify([path])
        assert lanes.run_python is True
        assert lanes.run_heavy_python is True
        assert lanes.run_docs is True
        assert lanes.run_security is True

    def test_py_change_is_not_all_unknown(self) -> None:
        lanes = classify(["src/teatree/core/x.py"])
        assert lanes.all is False

    @pytest.mark.parametrize(
        "path",
        [
            "src/teatree/anything.py",
            "tests/anything.py",
            "scripts/anything.py",
        ],
    )
    def test_any_source_or_tests_path_triggers_heavy(self, path: str) -> None:
        # No silent code-lane skip: any code path forces the full test lane.
        lanes = classify([path])
        assert lanes.run_heavy_python is True, f"{path!r} must trigger the heavy test lane"

    @pytest.mark.parametrize(
        "path",
        [
            "src/teatree/assets/logo.png",
            "tests/fixtures/sample.json",
            "scripts/data/blob.bin",
        ],
    )
    def test_non_py_file_under_code_dir_still_runs_heavy(self, path: str) -> None:
        # A non-python file under a code directory is unrecognised and
        # forces all=True — which still runs the heavy test lane. The
        # code lane is never wrongly skipped.
        lanes = classify([path])
        assert lanes.all is True
        assert lanes.run_heavy_python is True, f"{path!r} must not skip the heavy test lane"


class TestMixedDocsAndPython:
    def test_mixed_docs_and_py_runs_all_python(self) -> None:
        lanes = classify(["README.md", "src/teatree/core/x.py"])
        assert lanes.run_heavy_python is True
        assert lanes.run_python is True
        assert lanes.run_docs is True
        assert lanes.run_security is True
        assert lanes.all is False


class TestConfigForcesAll:
    @pytest.mark.parametrize("path", _CONFIG_PATHS)
    def test_config_change_forces_all(self, path: str) -> None:
        lanes = classify([path])
        assert lanes.all is True


class TestSecurityAlwaysRuns:
    @pytest.mark.parametrize(
        "paths",
        [
            _PURE_DOCS_PATHS,
            _PY_PATHS,
            _CONFIG_PATHS,
            ["mystery.unknownext"],
            ["README.md", "src/teatree/core/x.py"],
        ],
    )
    def test_security_runs_on_every_classification(self, paths: list[str]) -> None:
        lanes = classify(paths)
        assert lanes.run_security is True


class TestLaneSetsConsistency:
    def test_heavy_lanes_are_subset_of_python_lanes(self) -> None:
        assert HEAVY_PYTHON_LANES <= PYTHON_LANES

    def test_lane_sets_disjoint_from_docs(self) -> None:
        assert PYTHON_LANES.isdisjoint(DOCS_LANES)

    def test_all_implies_every_flag(self) -> None:
        lanes = Lanes(all=True)
        assert lanes.run_heavy_python is True
        assert lanes.run_python is True
        assert lanes.run_docs is True
        assert lanes.run_security is True


class TestMainCli:
    def test_main_emits_github_output(self, tmp_path, capsys) -> None:
        out_file = tmp_path / "gh_output"
        rc = main(["README.md"], output_path=str(out_file))
        assert rc == 0
        content = out_file.read_text(encoding="utf-8")
        assert "run_heavy_python=false" in content
        assert "run_docs=true" in content
        assert "run_security=true" in content
        assert "all=false" in content

    def test_main_unknown_path_writes_all_true(self, tmp_path) -> None:
        out_file = tmp_path / "gh_output"
        main(["mystery.zzz"], output_path=str(out_file))
        content = out_file.read_text(encoding="utf-8")
        assert "all=true" in content
        assert "run_heavy_python=true" in content

    def test_main_reads_paths_from_stdin(self, tmp_path, monkeypatch) -> None:
        out_file = tmp_path / "gh_output"
        monkeypatch.setattr("sys.stdin", io.StringIO("README.md\nBLUEPRINT.md\n"))
        rc = main([], output_path=str(out_file))
        assert rc == 0
        content = out_file.read_text(encoding="utf-8")
        assert "run_heavy_python=false" in content

    def test_main_prints_json_summary(self, tmp_path, capsys) -> None:
        out_file = tmp_path / "gh_output"
        main(["src/teatree/core/x.py"], output_path=str(out_file))
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["run_heavy_python"] is True
        assert payload["all"] is False
