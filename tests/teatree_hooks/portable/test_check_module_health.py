"""The whole-tree module-health DEBT report (souliane/teatree#3511).

The commit-time ratchet grandfathers over-cap files, so the standing debt stays
invisible until an unrelated PR inherits a split mid-task. ``run_debt_report``
makes the same set visible on demand — advisory, never blocking. Driven against a
controlled ``src/`` tree under ``tmp_path`` so the assertion is deterministic.
"""

import io
import os
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from teatree.hooks.portable.check_module_health import MAX_LOC, main, run_debt_report


def _lines(loc: int) -> str:
    return "\n".join(f"a_{i} = {i}" for i in range(loc)) + "\n"


def _seed_src(tmp_path: Path) -> Path:
    src = tmp_path / "src" / "teatree"
    src.mkdir(parents=True)
    return src


def test_debt_report_names_an_over_cap_module_and_never_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _seed_src(tmp_path)
    (src / "huge.py").write_text("\n".join(f"a_{i} = {i}" for i in range(MAX_LOC + 50)) + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = run_debt_report()

    out = buf.getvalue()
    assert rc == 0, "the debt report is advisory — it must never block"
    assert "huge.py" in out
    assert f"cap {MAX_LOC}" in out


def test_debt_report_says_none_on_a_clean_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _seed_src(tmp_path)
    (src / "small.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = run_debt_report()

    assert rc == 0
    assert "none" in buf.getvalue()


class TestStagedModeMeasuresTheVersionBeingCommitted:
    """The staged run judges the INDEX blob, not the working tree.

    The current side read the filesystem while the baseline came from ``git show
    HEAD:`` and the added-line map from ``git diff --cached`` — three snapshots,
    one of which is not part of any commit. An unstaged edit therefore decided
    whether a commit that does not contain it was blocked.
    """

    @staticmethod
    def _repo_with_staged_and_working(tmp_path: Path, *, staged_loc: int, working_loc: int) -> Path:
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }

        def git(*args: str) -> None:
            subprocess.run(
                ["git", *args],  # noqa: S607 — git from PATH is what the checked hook itself runs
                cwd=repo,
                check=True,
                capture_output=True,
                env=env,
            )

        target = repo / "src" / "big.py"
        git("init", "-b", "main")
        target.write_text("x = 0\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-m", "seed")
        target.write_text(_lines(staged_loc), encoding="utf-8")
        git("add", "src/big.py")
        target.write_text(_lines(working_loc), encoding="utf-8")
        return repo

    def test_an_unstaged_growth_does_not_block_a_clean_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._repo_with_staged_and_working(tmp_path, staged_loc=10, working_loc=MAX_LOC + 200)
        monkeypatch.chdir(repo)
        monkeypatch.setattr("sys.argv", ["check_module_health.py"])
        assert main() == 0

    def test_a_staged_over_cap_file_still_blocks_when_the_working_tree_looks_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._repo_with_staged_and_working(tmp_path, staged_loc=MAX_LOC + 200, working_loc=10)
        monkeypatch.chdir(repo)
        monkeypatch.setattr("sys.argv", ["check_module_health.py"])
        assert main() == 1
