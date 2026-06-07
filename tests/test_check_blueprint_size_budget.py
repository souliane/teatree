"""Tests for the BLUEPRINT corpus size-budget gate (#1128, #2040).

The gate keeps the BLUEPRINT architectural rather than a prose mirror of
the code. Its enforcement is two-tier (#2040).

Tier 1 is HARD, delta-based, race-free: a commit fails only when its own
diff (vs the merge-base with the base ref) grows the BLUEPRINT corpus
beyond a per-PR byte allowance. Concurrent growth of main between
branch-point and merge can never red a PR whose own diff is in allowance.

Tier 2 is WARN, absolute: when the merged corpus approaches the soft
budget the gate prints a loud "split a section into a linked appendix"
message and exits 0 — never a hard block.

``BLUEPRINT_SIZE_OVERRIDE=1`` remains the documented escape hatch for the
hard delta gate.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.hooks import check_blueprint_size_budget as gate

_GIT_BIN = shutil.which("git") or "/usr/bin/git"


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run([_GIT_BIN, "-C", str(repo), *args], check=True, capture_output=True)


def _write(repo: Path, relpath: str, content: str) -> None:
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real git repo seeded with a base BLUEPRINT corpus on ``main``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "config", "user.email", "t@example.com")
    _run_git(repo, "config", "user.name", "t")
    _write(repo, "BLUEPRINT.md", "x" * 1000)
    _write(repo, "docs/blueprint/configuration.md", "y" * 500)
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "base corpus")
    _run_git(repo, "checkout", "-q", "-b", "feature")
    return repo


def _commit_all(repo: Path, message: str) -> None:
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", message)


class TestDeltaHardGate:
    """The hard gate fails on the PR's OWN over-allowance corpus growth."""

    def test_own_diff_within_allowance_passes(self, git_repo: Path) -> None:
        _write(git_repo, "BLUEPRINT.md", "x" * (1000 + 100))
        _commit_all(git_repo, "small edit")
        assert gate.ci_main(repo=git_repo, base_ref="main") == 0

    def test_own_diff_over_top_level_allowance_fails(self, git_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write(git_repo, "BLUEPRINT.md", "x" * (1000 + gate._PER_PR_TOP_LEVEL_DELTA_BYTES + 1))
        _commit_all(git_repo, "huge top-level edit")
        assert gate.ci_main(repo=git_repo, base_ref="main") == 1
        captured = capsys.readouterr()
        assert "BLUEPRINT.md" in captured.out + captured.err

    def test_own_diff_over_total_allowance_fails(self, git_repo: Path) -> None:
        # Split the runaway growth across top-level + a new appendix so each
        # stays under the top-level allowance but the combined delta busts the
        # total allowance.
        half = gate._PER_PR_TOTAL_DELTA_BYTES // 2 + 1
        assert half <= gate._PER_PR_TOP_LEVEL_DELTA_BYTES
        _write(git_repo, "BLUEPRINT.md", "x" * (1000 + half))
        _write(git_repo, "docs/blueprint/loop-topology.md", "z" * half)
        _commit_all(git_repo, "split runaway growth")
        assert gate.ci_main(repo=git_repo, base_ref="main") == 1

    def test_reverting_delta_logic_flips_the_fail(self, git_repo: Path) -> None:
        # Anti-vacuity: an over-allowance own diff must be RED *because* of the
        # delta logic. A gate keyed on absolute size with a tiny base corpus
        # would pass — so this asserts the delta path is what reds it.
        _write(git_repo, "BLUEPRINT.md", "x" * (1000 + gate._PER_PR_TOP_LEVEL_DELTA_BYTES + 1))
        _commit_all(git_repo, "over-allowance own diff")
        top = (git_repo / "BLUEPRINT.md").stat().st_size
        assert top < gate._SOFT_TOP_LEVEL_BYTES
        assert gate.ci_main(repo=git_repo, base_ref="main") == 1


class TestRaceFreedom:
    """The defining #2040 regression: concurrent main growth must not red."""

    def test_concurrent_main_growth_does_not_red_innocent_pr(self, git_repo: Path) -> None:
        # The fixture leaves us on ``feature``, branched from the small base.
        # This PR's OWN diff is tiny.
        _write(git_repo, "BLUEPRINT.md", "x" * (1000 + 50))
        _commit_all(git_repo, "tiny own edit")
        # Meanwhile main grows enormously via concurrently merged PRs — far
        # past every absolute cap. The innocent PR's merge-base is still the
        # small base, so its OWN delta is 50 B.
        _run_git(git_repo, "checkout", "-q", "main")
        _write(git_repo, "BLUEPRINT.md", "x" * (gate._SOFT_TOP_LEVEL_BYTES + 50_000))
        _write(
            git_repo,
            "docs/blueprint/factory-architecture.md",
            "w" * (gate._SOFT_APPENDICES_BYTES + 50_000),
        )
        _commit_all(git_repo, "concurrent main growth")
        _run_git(git_repo, "checkout", "-q", "feature")
        assert gate.ci_main(repo=git_repo, base_ref="main") == 0


class TestWarnPath:
    def test_soft_threshold_emits_split_prompt(self, git_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # The base already carries a large appendix (over the soft threshold)
        # from prior reviewed merges. This PR branches from that large base and
        # adds a tiny edit: its OWN delta is within allowance (hard gate passes)
        # but the merged tree is over the soft threshold, so the gate WARNS
        # (exit 0) with the split-to-appendix prompt — never a hard block.
        _run_git(git_repo, "checkout", "-q", "main")
        _write(
            git_repo,
            "docs/blueprint/factory-architecture.md",
            "w" * (gate._SOFT_APPENDICES_BYTES + 10),
        )
        _commit_all(git_repo, "large appendix already on main")
        _run_git(git_repo, "checkout", "-q", "-b", "tiny-edit")
        _write(git_repo, "BLUEPRINT.md", "x" * (1000 + 100))
        _commit_all(git_repo, "small edit on already-large base")
        assert gate.ci_main(repo=git_repo, base_ref="main") == 0
        captured = capsys.readouterr()
        out = (captured.out + captured.err).lower()
        assert "split" in out
        assert "appendix" in out


class TestEscapeHatch:
    def test_env_override_skips_delta_gate(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(git_repo, "BLUEPRINT.md", "x" * (1000 + gate._PER_PR_TOP_LEVEL_DELTA_BYTES * 10))
        _commit_all(git_repo, "huge edit under override")
        monkeypatch.setenv("BLUEPRINT_SIZE_OVERRIDE", "1")
        assert gate.ci_main(repo=git_repo, base_ref="main") == 0

    def test_unchanged_blueprint_skips_check(self, git_repo: Path) -> None:
        _write(git_repo, "src/foo.py", "print('hi')\n")
        _commit_all(git_repo, "unrelated change")
        assert gate.ci_main(repo=git_repo, base_ref="main") == 0


class TestMergeBaseFallback:
    def test_unresolvable_base_ref_fails_open(self, git_repo: Path) -> None:
        # A non-existent base ref means the delta can't be computed; the gate
        # must fail OPEN (exit 0), never hard-block on an unresolvable base.
        _write(git_repo, "BLUEPRINT.md", "x" * (1000 + gate._PER_PR_TOP_LEVEL_DELTA_BYTES * 10))
        _commit_all(git_repo, "huge edit, bad base ref")
        assert gate.ci_main(repo=git_repo, base_ref="does/not/exist") == 0


class TestStagedTreeMain:
    """``main()`` runs the delta gate on the staged tree vs origin/main."""

    def test_blueprint_untouched_in_commit_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gate, "_blueprint_in_commit", lambda: False)
        assert gate.main() == 0

    def test_override_short_circuits_main(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BLUEPRINT_SIZE_OVERRIDE", "1")
        monkeypatch.setattr(gate, "_blueprint_in_commit", lambda: True)
        assert gate.main() == 0

    def test_blueprint_in_commit_reads_staged_diff(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _run_git(git_repo, "checkout", "-q", "main")
        _write(git_repo, "BLUEPRINT.md", "x" * 1200)
        _run_git(git_repo, "add", "BLUEPRINT.md")
        monkeypatch.chdir(git_repo)
        assert gate._blueprint_in_commit() is True

    def test_blueprint_in_commit_false_for_unrelated_stage(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(git_repo, "src/foo.py", "print('hi')\n")
        _run_git(git_repo, "add", "src/foo.py")
        monkeypatch.chdir(git_repo)
        assert gate._blueprint_in_commit() is False


class TestUnrelatedHistoryMergeBaseNone:
    """``_merge_base`` returns None when histories are unrelated → fail open."""

    def test_blueprint_changed_but_no_merge_base_fails_open(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _run_git(repo, "init", "-q", "-b", "main")
        _run_git(repo, "config", "user.email", "t@example.com")
        _run_git(repo, "config", "user.name", "t")
        _write(repo, "BLUEPRINT.md", "x" * 1000)
        _commit_all(repo, "main base")
        # An orphan branch with NO common ancestor: a huge BLUEPRINT but no
        # merge-base with main. The gate must fail OPEN, never hard-block.
        _run_git(repo, "checkout", "-q", "--orphan", "lonely")
        _run_git(repo, "rm", "-rfq", ".")
        _write(repo, "BLUEPRINT.md", "x" * (1000 + gate._PER_PR_TOP_LEVEL_DELTA_BYTES * 5))
        _commit_all(repo, "orphan huge blueprint")
        assert gate._merge_base(repo, "main") is None
        assert gate.ci_main(repo=repo, base_ref="main") == 0


class TestWarnPerCapBranches:
    def test_top_level_only_over_soft_warns(self, capsys: pytest.CaptureFixture[str]) -> None:
        gate._emit_warning(gate._SOFT_TOP_LEVEL_BYTES + 1, 0, gate._SOFT_TOP_LEVEL_BYTES + 1)
        assert "BLUEPRINT.md" in capsys.readouterr().err

    def test_total_only_over_soft_warns(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Neither per-file budget breached, only the combined total.
        gate._emit_warning(10, 10, gate._SOFT_TOTAL_BYTES + 1)
        assert "corpus total" in capsys.readouterr().err

    def test_no_warning_under_all_soft_thresholds(self, capsys: pytest.CaptureFixture[str]) -> None:
        gate._emit_warning(10, 10, 20)
        assert capsys.readouterr().err == ""


class TestGitSizeHelpersOnMissingRef:
    def test_show_size_zero_for_absent_path_at_ref(self, git_repo: Path) -> None:
        assert gate._git_show_size(git_repo, "main", "does/not/exist.md") == 0

    def test_ls_appendix_empty_when_dir_absent_at_ref(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _run_git(repo, "init", "-q", "-b", "main")
        _run_git(repo, "config", "user.email", "t@example.com")
        _run_git(repo, "config", "user.name", "t")
        _write(repo, "BLUEPRINT.md", "x" * 100)
        _commit_all(repo, "no appendix dir")
        assert gate._git_ls_appendix(repo, "main") == []


class TestAppendixTotalEmptyDir:
    def test_missing_appendix_dir_is_zero(self, tmp_path: Path) -> None:
        assert gate._appendix_total(tmp_path) == 0


class TestCliEntry:
    def test_cli_ci_mode_dispatches_to_ci_main(self, git_repo: Path) -> None:
        _write(git_repo, "BLUEPRINT.md", "x" * (1000 + 100))
        _commit_all(git_repo, "small edit")
        assert gate._cli_entry(["--ci", "--base-ref", "main", "--repo", str(git_repo)]) == 0

    def test_cli_default_mode_dispatches_to_main(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gate, "_blueprint_in_commit", lambda: False)
        assert gate._cli_entry([]) == 0


class TestRealCorpusDeltaIsBounded:
    """The live single-file BLUEPRINT.md exists; the gate resolves it."""

    def test_repo_root_points_at_blueprint(self) -> None:
        root = gate._repo_root()
        assert (root / "BLUEPRINT.md").exists()

    def test_soft_thresholds_are_above_per_pr_allowances(self) -> None:
        # A single PR's allowance must be a fraction of the soft budget, so the
        # warn signal precedes any per-PR cap, never the reverse.
        assert gate._PER_PR_TOP_LEVEL_DELTA_BYTES < gate._SOFT_TOP_LEVEL_BYTES
        assert gate._PER_PR_TOTAL_DELTA_BYTES < gate._SOFT_TOTAL_BYTES
