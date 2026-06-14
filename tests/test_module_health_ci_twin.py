"""base..head diff-mode for the module-health ratchet — the CI twin (#2010).

The prek hook runs ``check_module_health.py`` per-commit against staged files.
The CI lint job runs the prek commit stage, but no dedicated CI job re-runs the
ratchet on the PR's FULL diff. A prek-bypassing or merge-routed module growth
lands silently. The diff-mode (``--from-ref <base>``) lets the CI twin invoke
the ratchet on the whole ``base..HEAD`` range — not per-commit, so the
merge-commit false-positive the staged-mode exemption works around never fires.

These tests build a real ``tmp_path`` git repo (base commit + head commit) and
drive the diff-mode end to end, plus a structural assertion that the CI job
exists and invokes the script with the diff flag.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

import scripts.hooks.check_module_health as mod

_OVER_CAP = mod.MAX_LOC + 200
_OVER_CAP_GREW = _OVER_CAP + 50
_OVER_CAP_SHRANK = _OVER_CAP - 50

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_GIT_BIN = shutil.which("git") or "/usr/bin/git"


def _src(loc: int) -> str:
    return "\n".join(f"a_{i} = {i}" for i in range(loc)) + "\n"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [_GIT_BIN, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _commit_file(repo: Path, relpath: str, content: str, message: str) -> str:
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", relpath)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _load_ci_jobs() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"])


class TestDiffModeRatchet:
    """base..head diff-mode applies the same shrink ratchet over the PR range."""

    def test_over_cap_file_that_grows_across_range_is_flagged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        repo = _init_repo(tmp_path)
        base = _commit_file(repo, "src/big.py", _src(_OVER_CAP), "base: over-cap file")
        _commit_file(repo, "src/big.py", _src(_OVER_CAP_GREW), "head: grow the over-cap file")

        monkeypatch.chdir(repo)
        assert mod.run_diff_mode(base) == 1

    def test_over_cap_file_within_ratchet_across_range_passes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        repo = _init_repo(tmp_path)
        base = _commit_file(repo, "src/big.py", _src(_OVER_CAP), "base: over-cap file")
        _commit_file(repo, "src/big.py", _src(_OVER_CAP_SHRANK), "head: shrink the over-cap file")

        monkeypatch.chdir(repo)
        assert mod.run_diff_mode(base) == 0

    def test_file_newly_crossing_cap_across_range_is_flagged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        repo = _init_repo(tmp_path)
        base = _commit_file(repo, "src/grower.py", _src(mod.MAX_LOC - 50), "base: under cap")
        _commit_file(repo, "src/grower.py", _src(_OVER_CAP), "head: cross the cap")

        monkeypatch.chdir(repo)
        assert mod.run_diff_mode(base) == 1

    def test_unchanged_over_cap_file_outside_range_is_not_flagged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        repo = _init_repo(tmp_path)
        base = _commit_file(repo, "src/legacy.py", _src(_OVER_CAP), "base: grandfathered over-cap")
        _commit_file(repo, "src/other.py", _src(10), "head: unrelated small file")

        monkeypatch.chdir(repo)
        assert mod.run_diff_mode(base) == 0

    def test_diverged_base_ratchets_against_merge_base_not_base_tip(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Main diverged and independently shrank the file; the branch's own shrink must pass.

        Fork point: big.py = OVER_CAP. main (base_ref) independently shrinks it to
        OVER_CAP - 100; the feature branch shrinks it to OVER_CAP - 50 (a legitimate
        ratchet-COMPLIANT shrink vs the fork point). Ratcheting against the LITERAL
        base tip would report "up from OVER_CAP - 100" and false-block; ratcheting
        against the merge-base (the fork point) sees a shrink and passes.
        """
        repo = _init_repo(tmp_path)
        fork_point = _commit_file(repo, "src/big.py", _src(_OVER_CAP), "fork: over-cap file")

        _git(repo, "checkout", "-q", "-b", "mainline")
        _commit_file(repo, "src/big.py", _src(_OVER_CAP - 100), "mainline: independent shrink")

        _git(repo, "checkout", "-q", fork_point)
        _git(repo, "checkout", "-q", "-b", "feature")
        _commit_file(repo, "src/big.py", _src(_OVER_CAP - 50), "feature: legitimate shrink")

        monkeypatch.chdir(repo)
        assert mod.run_diff_mode("mainline") == 0


class TestDiffModeArgv:
    """``--from-ref`` selects diff-mode; bare argv keeps staged-mode."""

    def test_from_ref_argv_runs_diff_mode(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        base = _commit_file(repo, "src/big.py", _src(_OVER_CAP), "base: over-cap file")
        _commit_file(repo, "src/big.py", _src(_OVER_CAP_GREW), "head: grow the over-cap file")

        monkeypatch.chdir(repo)
        monkeypatch.setattr("sys.argv", ["check_module_health.py", "--from-ref", base])
        assert mod.main() == 1


class TestCiTwinJobStructure:
    """The CI job exists and invokes the script on the PR's base..head range."""

    def test_module_health_gate_job_exists(self) -> None:
        assert "module-health-gate" in _load_ci_jobs(), (
            "ci.yml must define a module-health-gate job that re-runs the ratchet "
            "on the PR's full diff (the bypass-proof CI twin)."
        )

    def test_job_invokes_script_with_from_ref(self) -> None:
        job = _load_ci_jobs()["module-health-gate"]
        runs = " ".join(step.get("run", "") for step in job["steps"] if isinstance(step, dict))
        assert "check_module_health.py" in runs, "module-health-gate must invoke check_module_health.py."
        assert "--from-ref" in runs, (
            "module-health-gate must pass --from-ref so the ratchet runs on the base..head range, not per-commit."
        )

    def test_job_is_pr_only_with_full_history(self) -> None:
        job = _load_ci_jobs()["module-health-gate"]
        assert job.get("if") == "github.event_name == 'pull_request'", (
            "module-health-gate must be PR-only — there is no base to diff on push/schedule."
        )
        checkout = next(
            step for step in job["steps"] if isinstance(step, dict) and "checkout" in str(step.get("uses", ""))
        )
        assert checkout.get("with", {}).get("fetch-depth") == 0, (
            "module-health-gate must checkout with fetch-depth: 0 so origin/<base>...HEAD resolves."
        )
