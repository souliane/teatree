"""The docs-drift gate must actually catch stale ``cli-reference.md``.

souliane/teatree#2599 bug 1: ``generate_cli_reference.py`` unconditionally
``git add``-ed its own output, so the CI ``git diff --exit-code docs/generated``
(no ``--cached``) compared working-tree-vs-index and saw nothing — real drift
shipped undetected. The fix mirrors the antipattern-catalog gate: a
``CLI_REFERENCE_NO_STAGE`` opt-out (set in CI so the diff gate stays loud) plus a
loud sync checker that fails on local drift.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from tests._git_repo import make_git_repo, run_git

# Each subprocess-spawning class below runs the cli-reference generator/sync
# checker, a full Django bootstrap that stretches past the 60s default
# pytest-timeout under concurrent-coder load; give them headroom.
_SCAN_TIMEOUT = pytest.mark.timeout(300)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GENERATOR = _REPO_ROOT / "scripts" / "hooks" / "generate_cli_reference.py"
_SYNC_CHECKER = _REPO_ROOT / "scripts" / "hooks" / "check_cli_reference_sync.py"
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load_ci_jobs() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"])


def _docs_drift_steps() -> list[dict[str, Any]]:
    return [s for s in _load_ci_jobs()["docs-drift"]["steps"] if isinstance(s, dict)]


def _run(script: Path, *args: str, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "teatree.settings"}
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@_SCAN_TIMEOUT
class TestGeneratorNoStageOptOut:
    """With CLI_REFERENCE_NO_STAGE set, the generator writes but never git-adds."""

    def test_committed_reference_is_in_sync(self, tmp_path: Path) -> None:
        out = tmp_path / "cli-reference.md"
        result = _run(_GENERATOR, str(out), env_overrides={"CLI_REFERENCE_NO_STAGE": "1"})
        assert result.returncode == 0, result.stderr
        committed = (_REPO_ROOT / "docs" / "generated" / "cli-reference.md").read_text(encoding="utf-8")
        assert out.read_text(encoding="utf-8") == committed


@_SCAN_TIMEOUT
class TestDocsDriftGateCatchesStaleReference:
    """End-to-end: the working-tree-vs-index diff catches a stale committed doc.

    This is the bug-1 contract. The generator regenerates the doc; with
    ``CLI_REFERENCE_NO_STAGE`` the regenerated file lands in the working tree but
    NOT the index, so ``git diff`` (no ``--cached``) sees the drift. Without the
    opt-out (the old behaviour) the generator git-adds its own output, the index
    matches the working tree, and the same diff is empty — masking the drift.
    """

    def _seed_repo(self, tmp_path: Path) -> Path:
        repo = make_git_repo(tmp_path / "repo")
        doc_dir = repo / "docs" / "generated"
        doc_dir.mkdir(parents=True)
        committed = (_REPO_ROOT / "docs" / "generated" / "cli-reference.md").read_text(encoding="utf-8")
        # Stale the committed doc, then commit it — drift the gate must catch.
        stale = committed + "\nstale drift line that the live CLI does not produce\n"
        (doc_dir / "cli-reference.md").write_text(stale, encoding="utf-8")
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-qm", "seed")
        return repo

    def _regen_into(self, repo: Path, *, no_stage: bool) -> None:
        out = repo / "docs" / "generated" / "cli-reference.md"
        env_overrides = {"CLI_REFERENCE_NO_STAGE": "1"} if no_stage else {}
        result = _run(_GENERATOR, str(out), env_overrides=env_overrides)
        assert result.returncode == 0, result.stderr

    def _diff_is_clean(self, repo: Path) -> bool:
        # ``run_git(check=False)`` swallows the exit code; the empty/non-empty
        # stdout of a plain ``diff`` is the equivalent signal.
        return run_git(repo, "diff", "docs/generated", check=False) == ""

    def test_gate_red_on_drift_with_no_stage(self, tmp_path: Path) -> None:
        repo = self._seed_repo(tmp_path)
        self._regen_into(repo, no_stage=True)
        assert not self._diff_is_clean(repo), "docs-drift gate must catch a stale committed reference"

    def test_gate_green_when_in_sync_with_no_stage(self, tmp_path: Path) -> None:
        repo = self._seed_repo(tmp_path)
        # Regenerate twice: once to fix the stale file in the working tree, then
        # stage the now-correct file (the developer's intentional `git add`).
        self._regen_into(repo, no_stage=True)
        run_git(repo, "add", "docs/generated/cli-reference.md")
        self._regen_into(repo, no_stage=True)
        assert self._diff_is_clean(repo), "an in-sync committed reference must pass the gate"


@_SCAN_TIMEOUT
class TestSyncCheckerFiresOnDrift:
    """The loud sync checker detects a stale committed reference."""

    def test_passes_when_in_sync(self) -> None:
        result = _run(_SYNC_CHECKER)
        assert result.returncode == 0, f"sync checker should pass on a fresh tree:\n{result.stdout}\n{result.stderr}"

    def test_fails_when_committed_reference_is_stale(self, tmp_path: Path) -> None:
        # Run the checker against a worktree whose committed doc is stale.
        repo = tmp_path / "repo"
        doc_dir = repo / "docs" / "generated"
        doc_dir.mkdir(parents=True)
        (doc_dir / "cli-reference.md").write_text("# CLI Reference\n\nstale\n", encoding="utf-8")
        # The checker resolves _DOC relative to its own location, so copy the
        # script into the throwaway tree and run it there.
        scripts_dir = repo / "scripts" / "hooks"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "check_cli_reference_sync.py").write_text(_SYNC_CHECKER.read_text(encoding="utf-8"))
        result = _run(scripts_dir / "check_cli_reference_sync.py")
        assert result.returncode == 1, f"checker must go red on stale doc:\n{result.stdout}"


@_SCAN_TIMEOUT
class TestSyncCheckerUnmaskableByGenerator:
    """The sync checker catches a stale COMMITTED doc even after the generator runs.

    souliane/teatree#2599: the gate's correctness must not hinge on the generator
    NOT having repaired the working tree first (the generate-before-check
    vacuousness class). ``check_cli_reference_sync.py`` re-renders in memory and
    compares against the doc as it sits in the git INDEX — the bytes that will be
    committed — so a working-tree regeneration by ``generate_cli_reference.py``
    (which, under ``CLI_REFERENCE_NO_STAGE``, never stages) cannot mask the stale
    committed bytes the gate exists to catch.
    """

    def _seed_stale_committed_repo(self, tmp_path: Path) -> Path:
        repo = make_git_repo(tmp_path / "repo")
        (repo / "docs" / "generated").mkdir(parents=True)
        (repo / "scripts" / "hooks").mkdir(parents=True)
        fresh = (_REPO_ROOT / "docs" / "generated" / "cli-reference.md").read_text(encoding="utf-8")
        stale = fresh + "\nstale drift line the live CLI does not produce\n"
        doc = repo / "docs" / "generated" / "cli-reference.md"
        # Commit the STALE doc: index + HEAD hold the drifted bytes.
        doc.write_text(stale, encoding="utf-8")
        (repo / "scripts" / "hooks" / "check_cli_reference_sync.py").write_text(
            _SYNC_CHECKER.read_text(encoding="utf-8"), encoding="utf-8"
        )
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-qm", "seed stale committed doc")
        # Repair the WORKING-TREE doc to the fresh render — exactly what
        # generate_cli_reference.py does under CLI_REFERENCE_NO_STAGE (write, do not
        # stage) — so the index keeps the stale bytes while the working tree looks clean.
        doc.write_text(fresh, encoding="utf-8")
        return repo

    def test_checker_catches_stale_committed_doc_after_worktree_repaired(self, tmp_path: Path) -> None:
        repo = self._seed_stale_committed_repo(tmp_path)
        # A working-tree read sees the repaired (fresh) doc and passes vacuously;
        # reading the committed index must still go red on the stale committed bytes.
        result = _run(repo / "scripts" / "hooks" / "check_cli_reference_sync.py")
        assert result.returncode == 1, (
            "sync checker must catch a stale COMMITTED doc even after the working tree "
            f"was repaired to the fresh render (generate-before-check vacuousness):\n{result.stdout}"
        )


class TestDocsDriftCiUnmasksCliReference:
    """The docs-drift CI job must run the cli-reference generator with NO_STAGE."""

    def test_sync_checker_runs_in_docs_drift(self) -> None:
        joined = " ".join(str(s.get("run", "")) for s in _docs_drift_steps())
        assert "check_cli_reference_sync.py" in joined, (
            "docs-drift must run the in-memory cli-reference sync checker so a stale "
            "committed reference fails the gate independent of git-add/diff semantics."
        )

    def test_generator_runs_before_diff_assertion(self) -> None:
        runs = [s.get("run", "") for s in _docs_drift_steps()]
        gen_idx = next(i for i, r in enumerate(runs) if "generate_cli_reference.py" in r)
        diff_idx = next(i for i, r in enumerate(runs) if "git diff --exit-code docs/generated" in r)
        assert gen_idx < diff_idx, (
            "generate_cli_reference.py must run BEFORE the docs/generated diff "
            "assertion, else the gate has nothing to catch."
        )

    def test_generator_step_sets_no_stage_env(self) -> None:
        gen_steps = [s for s in _docs_drift_steps() if "generate_cli_reference.py" in str(s.get("run", ""))]
        assert gen_steps, "docs-drift must run the cli-reference generator."
        env = gen_steps[0].get("env", {})
        assert str(env.get("CLI_REFERENCE_NO_STAGE", "")) == "1", (
            "The cli-reference generator step must set CLI_REFERENCE_NO_STAGE=1 so it "
            "does not git-add the regenerated file (which would hide drift from "
            "`git diff` with no --cached)."
        )
