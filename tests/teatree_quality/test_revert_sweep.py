"""The revert-each-hunk sweep reports an unpinned hunk as a SURVIVOR and goes clean once a test pins it."""

import json
import subprocess
import sys
from pathlib import Path
from shutil import which

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.quality.revert_sweep import (
    HunkStatus,
    RevertSweepError,
    in_scope,
    measure_revert_sweep,
    read_revert_sweep,
    receipt_path,
    write_revert_sweep,
)

PYTEST_IN_TINY_REPO = (
    sys.executable,
    "-m",
    "pytest",
    "-q",
    "-x",
    "-p",
    "no:cacheprovider",
    "--disable-plugin-autoload",
    "-o",
    "addopts=",
)

_PROBE_SCRIPT = (
    "import os, sys; "
    "outer = ('COV_CORE_', 'COVERAGE_', 'PYTEST_XDIST_'); "
    "isolated = os.environ.get('PYTHONDONTWRITEBYTECODE') == '1' and os.environ['PYTHONPATH'].endswith('src') "
    "and not any(key.startswith(outer) for key in os.environ); "
    "sys.exit(0 if isolated else 3)"
)
INNER_ENV_PROBE = (sys.executable, "-c", _PROBE_SCRIPT)

BASE_SOURCE = """def allowed(count: int) -> bool:
    return count < 3


def label() -> str:
    return "ok"
"""

HEAD_SOURCE = """def allowed(count: int) -> bool:
    return count < 5


def label() -> str:
    return "fine"
"""

PINS_ALLOWED = """from teatree.core.gates.limit import allowed


def test_boundary() -> None:
    assert allowed(4)
    assert not allowed(5)
"""

PINS_LABEL = """from teatree.core.gates.limit import label


def test_label() -> None:
    assert label() == "fine"
"""


def _git(repo: Path, *args: str) -> str:
    executable = which("git") or "git"
    done = subprocess.run(
        [executable, "-c", "user.name=Test", "-c", "user.email=fixture.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _write(repo: Path, relative: str, text: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "tiny"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for package in ("src/teatree", "src/teatree/core", "src/teatree/core/gates"):
        _write(repo, f"{package}/__init__.py", "")
    _write(repo, "src/teatree/core/gates/limit.py", BASE_SOURCE)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "src/teatree/core/gates/limit.py", HEAD_SOURCE)
    _write(repo, "tests/teatree_core/gates/test_limit.py", PINS_ALLOWED)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change limit and label")
    return repo


def _measure(repo: Path):
    return measure_revert_sweep(
        repo,
        base_ref="origin/main",
        expect_sha=_git(repo, "rev-parse", "HEAD"),
        test_command=PYTEST_IN_TINY_REPO,
    )


def test_unpinned_hunk_is_a_survivor_and_pinned_hunk_is_killed(tiny_repo: Path) -> None:
    sweep = _measure(tiny_repo)

    by_line = {verdict.line: verdict.status for verdict in sweep.verdicts}
    assert by_line == {2: HunkStatus.KILLED, 6: HunkStatus.SURVIVOR}
    assert [verdict.line for verdict in sweep.survivors] == [6]
    assert not sweep.clean


def test_no_survivors_once_a_test_pins_every_hunk(tiny_repo: Path) -> None:
    _write(tiny_repo, "tests/teatree_core/gates/test_limit_label.py", PINS_LABEL)
    _git(tiny_repo, "add", "-A")
    _git(tiny_repo, "commit", "-qm", "pin label")

    sweep = _measure(tiny_repo)

    assert sweep.survivors == ()
    assert sweep.clean


def test_sweep_leaves_the_measured_checkout_untouched(tiny_repo: Path) -> None:
    _measure(tiny_repo)

    assert _git(tiny_repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert (tiny_repo / "src/teatree/core/gates/limit.py").read_text(encoding="utf-8") == HEAD_SOURCE
    assert "revert-sweep" not in _git(tiny_repo, "worktree", "list")


def test_receipt_round_trips_at_its_head(tiny_repo: Path) -> None:
    sweep = _measure(tiny_repo)

    write_revert_sweep(tiny_repo, sweep)

    assert read_revert_sweep(tiny_repo) == sweep
    assert receipt_path(tiny_repo).is_relative_to(tiny_repo / ".git")


def test_receipt_absent_reads_as_none(tiny_repo: Path) -> None:
    assert read_revert_sweep(tiny_repo) is None


def test_malformed_receipt_fails_loud(tiny_repo: Path) -> None:
    path = receipt_path(tiny_repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"head_sha": 3}), encoding="utf-8")

    with pytest.raises(RevertSweepError):
        read_revert_sweep(tiny_repo)


def test_hunk_without_any_test_to_run_is_unmeasured_and_not_clean(tiny_repo: Path) -> None:
    _git(tiny_repo, "rm", "-q", "tests/teatree_core/gates/test_limit.py")
    _git(tiny_repo, "commit", "-qm", "drop the only test")

    sweep = _measure(tiny_repo)

    assert {verdict.status for verdict in sweep.verdicts} == {HunkStatus.UNMEASURED}
    assert not sweep.clean


def test_more_than_forty_hunks_fails_loudly(tiny_repo: Path) -> None:
    base_lines = "\n".join(f"V{index} = {index}" for index in range(100))
    _write(tiny_repo, "src/teatree/core/gates/many.py", base_lines + "\n")
    _git(tiny_repo, "add", "-A")
    _git(tiny_repo, "commit", "-qm", "many base")
    _git(tiny_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    changed = "\n".join(f"V{index} = {index + 1 if index % 2 == 0 else index}" for index in range(100))
    _write(tiny_repo, "src/teatree/core/gates/many.py", changed + "\n")
    _git(tiny_repo, "add", "-A")
    _git(tiny_repo, "commit", "-qm", "touch fifty lines")

    with pytest.raises(RevertSweepError, match="40"):
        _measure(tiny_repo)


def test_a_worktree_head_other_than_the_expected_sha_refuses(tiny_repo: Path) -> None:
    with pytest.raises(RevertSweepError, match="expected"):
        measure_revert_sweep(
            tiny_repo,
            base_ref="origin/main",
            expect_sha=_git(tiny_repo, "rev-parse", "HEAD~1"),
            test_command=PYTEST_IN_TINY_REPO,
        )


def test_an_uncommitted_change_to_a_tracked_file_refuses(tiny_repo: Path) -> None:
    _write(tiny_repo, "src/teatree/core/gates/limit.py", HEAD_SOURCE + "\nEDITED = True\n")

    with pytest.raises(RevertSweepError, match="committed tracked files"):
        _measure(tiny_repo)


def test_the_inner_run_is_isolated_from_the_outer_test_session(
    tiny_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/outer/session/path")
    monkeypatch.setenv("COV_CORE_SOURCE", "src/teatree")
    monkeypatch.setenv("COVERAGE_FILE", "/outer/.coverage")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")

    sweep = measure_revert_sweep(
        tiny_repo,
        base_ref="origin/main",
        expect_sha=_git(tiny_repo, "rev-parse", "HEAD"),
        test_command=INNER_ENV_PROBE,
    )

    assert {verdict.status for verdict in sweep.verdicts} == {HunkStatus.SURVIVOR}


def test_tests_that_fail_before_any_revert_refuse_with_their_output(tiny_repo: Path) -> None:
    failing = (sys.executable, "-c", "import sys; print('collected nothing useful'); sys.exit(3)")

    with pytest.raises(RevertSweepError, match="collected nothing useful"):
        measure_revert_sweep(
            tiny_repo,
            base_ref="origin/main",
            expect_sha=_git(tiny_repo, "rev-parse", "HEAD"),
            test_command=failing,
        )


def test_comment_only_hunk_is_inert_and_not_swept(tiny_repo: Path) -> None:
    _write(tiny_repo, "src/teatree/core/gates/limit.py", HEAD_SOURCE + "\n# a trailing note\n")
    _git(tiny_repo, "add", "-A")
    _git(tiny_repo, "commit", "-qm", "comment")

    sweep = _measure(tiny_repo)

    assert sorted(verdict.line for verdict in sweep.verdicts) == [2, 6]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/teatree/core/merge/head_overlap.py", True),
        ("src/teatree/core/gates/merge_guard.py", True),
        ("src/teatree/core/cleanup/cleanup.py", True),
        ("src/teatree/core/retention/prune.py", True),
        ("src/teatree/docker/reap.py", True),
        ("src/teatree/core/models/merge_clear.py", True),
        ("src/teatree/core/models/review_verdict.py", True),
        ("src/teatree/core/models/ticket.py", False),
        ("tests/teatree_core/gates/test_merge_guard.py", False),
        ("src/teatree/core/gates/README.md", False),
    ],
)
def test_scope_covers_the_named_production_areas(*, path: str, expected: bool) -> None:
    assert in_scope(path) is expected


def test_cli_exits_nonzero_with_the_survivor_then_zero_once_pinned(
    tiny_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tiny_repo)
    head = _git(tiny_repo, "rev-parse", "HEAD")
    arguments = ["tool", "revert-sweep", "--expect-sha", head, "--test-command", " ".join(PYTEST_IN_TINY_REPO)]

    unpinned = CliRunner().invoke(app, arguments)

    assert unpinned.exit_code == 1
    assert "SURVIVOR" in unpinned.output
    assert "limit.py:6" in unpinned.output

    _write(tiny_repo, "tests/teatree_core/gates/test_limit_label.py", PINS_LABEL)
    _git(tiny_repo, "add", "-A")
    _git(tiny_repo, "commit", "-qm", "pin label")
    pinned_head = _git(tiny_repo, "rev-parse", "HEAD")

    pinned = CliRunner().invoke(app, [*arguments[:3], pinned_head, *arguments[4:]])

    assert pinned.exit_code == 0, pinned.output
    assert "SURVIVOR" not in pinned.output
    assert read_revert_sweep(tiny_repo) is not None
