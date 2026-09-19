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

import teatree.hooks.portable.check_module_health as mod
from teatree.hooks.portable.check_module_health import MAX_LOC, main, over_cap_growth, run_debt_report


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


class TestOverCapGrowthPredicate:
    """One predicate answers "will the shrink ratchet refuse this?" for both callers.

    The commit-stage ratchet and the edit-time advisory must never disagree about
    when a growth is refused, so they share :func:`over_cap_growth` rather than
    each re-deriving the cap comparison.
    """

    def test_growth_above_the_cap_is_reported_with_its_net_delta(self) -> None:
        growth = over_cap_growth(
            "src/teatree/big.py",
            source=_lines(MAX_LOC + 13),
            baseline_source=_lines(MAX_LOC + 3),
        )

        assert growth is not None
        assert growth.loc == MAX_LOC + 13
        assert growth.baseline_loc == MAX_LOC + 3
        assert growth.net_growth == 10
        assert growth.cap == MAX_LOC

    def test_an_over_cap_file_that_shrinks_is_not_a_growth(self) -> None:
        assert (
            over_cap_growth(
                "src/teatree/big.py",
                source=_lines(MAX_LOC + 3),
                baseline_source=_lines(MAX_LOC + 13),
            )
            is None
        )

    def test_an_over_cap_file_held_steady_is_not_a_growth(self) -> None:
        steady = _lines(MAX_LOC + 5)
        assert over_cap_growth("src/teatree/big.py", source=steady, baseline_source=steady) is None

    def test_a_file_crossing_the_cap_for_the_first_time_is_not_a_ratchet_growth(self) -> None:
        """A newly-over-cap file gets the `Split by concern` message, not extract-first."""
        assert (
            over_cap_growth(
                "src/teatree/big.py",
                source=_lines(MAX_LOC + 2),
                baseline_source=_lines(MAX_LOC - 40),
            )
            is None
        )

    def test_a_non_first_party_path_is_never_a_growth(self) -> None:
        assert (
            over_cap_growth(
                "tests/teatree_core/test_big.py",
                source=_lines(MAX_LOC + 13),
                baseline_source=_lines(MAX_LOC + 3),
            )
            is None
        )

    def test_a_migration_is_exempt_like_the_rest_of_the_ratchet(self) -> None:
        assert (
            over_cap_growth(
                "src/teatree/core/migrations/0001_initial.py",
                source=_lines(MAX_LOC + 13),
                baseline_source=_lines(MAX_LOC + 3),
            )
            is None
        )


class TestOverCapGrowthMessageNamesTheDeficit:
    """The refusal quantifies the deficit and prescribes extract-first (#2663)."""

    def _blocked_output(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
        target = tmp_path / "src" / "teatree" / "big.py"
        target.parent.mkdir(parents=True)
        target.write_text(_lines(MAX_LOC + 13), encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)  # noqa: S607 — fixture drives the same git the hook resolves
        monkeypatch.setattr(mod, "_staged_python_files", lambda: ["src/teatree/big.py"])
        monkeypatch.setattr(mod, "_head_paths", lambda: {"src/teatree/big.py": "src/teatree/big.py"})
        monkeypatch.setattr(mod, "_staged_source", lambda _p: _lines(MAX_LOC + 13))
        monkeypatch.setattr(mod, "_count_loc_at_head", lambda _p: MAX_LOC + 3)
        monkeypatch.setattr(mod, "_count_module_level_functions_at_head", lambda _p: [])
        monkeypatch.setattr(mod, "_added_line_numbers", lambda _f, _h: set())
        monkeypatch.setattr("sys.argv", ["check_module_health.py"])

        buf = io.StringIO()
        with redirect_stdout(buf):
            assert mod.main() == 1
        return buf.getvalue()

    def test_refusal_states_the_net_growth_the_extraction_must_offset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = self._blocked_output(tmp_path, monkeypatch)

        assert "net +10" in out, "the refusal must quantify how much must come out"
        assert "extract" in out.lower(), "the refusal must prescribe the extract-first remedy"
        assert "docs/module-health.md" in out, "the refusal must point at the durable rule"

    def test_refusal_keeps_the_pinned_shrink_only_wording(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Other gates and evals key on this substring — quantifying must not drop it."""
        assert "Over-cap files may only shrink" in self._blocked_output(tmp_path, monkeypatch)
