"""Review evidence stays bound to the exact head and fails loud when stale."""

import subprocess
from pathlib import Path
from shutil import which
from unittest.mock import patch

from teatree.quality.duplicate_definitions import DuplicateDefinition
from teatree.quality.kill_matrix import KillMatrix, KillRow
from teatree.quality.review_evidence import (
    ReviewEvidence,
    imported_modules_for_tests,
    measure_review_evidence,
    read_review_evidence,
    write_review_evidence,
)


def _git(repo: Path, *args: str) -> None:
    executable = which("git")
    if executable is None:
        message = "git executable unavailable"
        raise RuntimeError(message)
    subprocess.run([executable, *args], cwd=repo, check=True, capture_output=True)


def test_receipt_is_read_only_for_its_measured_head(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "file.txt").write_text("one")
    _git(tmp_path, "add", "file.txt")
    _git(tmp_path, "-c", "user.name=Test", "-c", "user.email=fixture.invalid", "commit", "-qm", "one")
    head = subprocess.run(
        [which("git") or "git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    evidence = ReviewEvidence(
        head,
        (DuplicateDefinition("src/a.py", "A.run", 2, 4),),
        KillMatrix(head, (KillRow("tests/test_a.py::test_one", ("m1",), (), (), ()),)),
    )
    write_review_evidence(tmp_path, evidence)
    loaded = read_review_evidence(tmp_path, head)
    assert loaded == evidence
    assert read_review_evidence(tmp_path, "b" * 40) is None


def test_measurement_scans_diff_and_passes_exact_changed_test_nodes(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "fixture.invalid")
    source = tmp_path / "src" / "teatree" / "sample.py"
    source.parent.mkdir(parents=True)
    source.write_text("def one():\n    return 1\n")
    _git(tmp_path, "add", "src/teatree/sample.py")
    _git(tmp_path, "commit", "-qm", "base")
    source.write_text("def one():\n    return 1\ndef one():\n    return 2\n")
    test_file = tmp_path / "tests" / "test_sample.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_one():\n    assert True\n")
    _git(tmp_path, "add", "src/teatree/sample.py", "tests/test_sample.py")
    _git(tmp_path, "commit", "-qm", "change")
    head = subprocess.run(
        [which("git") or "git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    matrix = KillMatrix(head, (KillRow("tests/test_sample.py::test_one", ("m1",), (), (), ()),))
    with (
        patch(
            "teatree.quality.review_evidence.collect_changed_test_nodes",
            return_value=("tests/test_sample.py::test_one",),
        ),
        patch("teatree.quality.review_evidence.build_kill_matrix", return_value=matrix) as build,
    ):
        evidence = measure_review_evidence(tmp_path, base_ref="HEAD^", expect_sha=head)
    assert evidence.duplicates[0].name == "one"
    build.assert_called_once_with(("tests/test_sample.py::test_one",), ("src/teatree/sample.py",), head_sha=head)


def test_test_only_change_uses_the_imported_source_module(tmp_path: Path) -> None:
    module = tmp_path / "src" / "teatree" / "core" / "widget.py"
    module.parent.mkdir(parents=True)
    module.write_text("def widget():\n    return 1\n")
    test_file = tmp_path / "tests" / "test_widget.py"
    test_file.parent.mkdir()
    test_file.write_text("from teatree.core.widget import widget\n\ndef test_widget():\n    assert widget() == 1\n")
    assert imported_modules_for_tests(tmp_path, ("tests/test_widget.py",)) == ("src/teatree/core/widget.py",)
