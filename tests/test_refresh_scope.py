"""Tests for the lock-refresh path-confinement gate and its workflow wiring (#4569).

Issue #4490 was a lockfile bump in name only: 108 ``src/`` and 80 ``tests/`` files were pushed
onto the refresh branch to make it green, and GitHub-native auto-merge — armed at PR creation
from ``lock_delta.py``'s patch-only VERSION verdict — landed all of it unreviewed. A version
classifier cannot see paths, and nothing re-evaluated the arming after the branch drifted.

The broken case and the working one are indistinguishable from the outside: both are a green
PR with auto-merge armed, and only the changed-path set tells them apart. So the tests that
carry the proof CONSTRUCT both diffs as real git branches rather than read the workflow — the
YAML assertions at the bottom are supporting evidence, never the verdict.

``scripts/ci/refresh_scope.py`` imports no ``teatree``: it runs in the lock-refresh job, which
does ``uv lock --upgrade`` and deliberately no ``uv sync`` (the constraint
``scripts/ci/audit_canary.py`` documents), so reaching into the package would add a full
install to a job that needs none.
"""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from scripts.ci.refresh_scope import MARKER, decide, escaping, in_scope, main, render_hold_comment

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "refresh_scope.py"
_REFRESH_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "uv-lock-upgrade.yml"
_SCOPE_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "lock-refresh-scope.yml"

_BRANCH_PREFIX = "chore/uv-lock-upgrade"
_GENERATED = ("uv.lock", "dist/sbom.json", "docs/generated/cli-reference.md")
_PR_STEP = "Open or update the lock-refresh PR"


class TestAllowlist:
    @pytest.mark.parametrize(
        "paths",
        [
            ("uv.lock",),
            ("uv.lock", "dist/sbom.json"),
            _GENERATED,
            ("docs/generated/nested/deep/page.md",),
        ],
    )
    def test_lock_and_generated_artifacts_are_in_scope(self, paths: tuple[str, ...]) -> None:
        assert escaping(paths) == ()
        assert in_scope(paths) is True

    @pytest.mark.parametrize(
        "path",
        [
            "src/teatree/loop/scanners/pr_sweep.py",
            "tests/test_lock_delta.py",
            "pyproject.toml",
            ".github/workflows/ci.yml",
            "docs/index.md",
            "docs/generated-notes.md",
            "uv.lock.bak",
            "dist/sbom.json.orig",
        ],
    )
    def test_a_single_escaping_path_withholds_auto_merge(self, path: str) -> None:
        paths = (*_GENERATED, path)
        assert escaping(paths) == (path,)
        assert in_scope(paths) is False

    def test_an_empty_diff_is_held_not_waved_through(self) -> None:
        assert in_scope(()) is False
        assert "not a lockfile bump" in decide(()).reason

    def test_blank_entries_are_dropped_before_the_verdict(self) -> None:
        assert in_scope(("uv.lock", "", "  ")) is True

    def test_escaping_paths_are_deduplicated_and_sorted(self) -> None:
        assert escaping(("tests/b.py", "src/a.py", "src/a.py")) == ("src/a.py", "tests/b.py")


class TestExpectedCount:
    def test_a_matching_count_is_accepted(self) -> None:
        assert decide(_GENERATED, expected_count=3).in_scope is True

    def test_a_truncated_file_list_is_held_even_when_every_path_read_is_in_scope(self) -> None:
        verdict = decide(_GENERATED, expected_count=191)
        assert verdict.in_scope is False
        assert verdict.escaping == ()
        assert "191" in verdict.reason


class TestHoldComment:
    def test_it_carries_the_upsert_marker_and_names_every_escaping_path(self) -> None:
        body = render_hold_comment(decide((*_GENERATED, "src/teatree/app.py", "tests/test_app.py")))
        assert body.startswith(MARKER)
        assert "`src/teatree/app.py`" in body
        assert "`tests/test_app.py`" in body

    def test_a_large_escape_set_is_capped_and_the_remainder_counted(self) -> None:
        body = render_hold_comment(decide(("uv.lock", *(f"src/mod_{index:03d}.py" for index in range(50)))))
        assert "and 30 more" in body

    def test_the_reason_distinguishes_it_from_the_4548_stall(self) -> None:
        body = render_hold_comment(decide(("uv.lock", "src/teatree/app.py")))
        assert "4548" in body, (
            "A held refresh PR superficially resembles the #4548 sbom/docs-drift stall; the "
            "comment must say which one this is or it reads as the silent failure."
        )

    def test_a_truncated_listing_explains_itself_without_naming_paths(self) -> None:
        body = render_hold_comment(decide(_GENERATED, expected_count=191))
        assert "191" in body
        assert "Paths outside" not in body


_GIT = shutil.which("git") or "git"
_FAKE_EMAIL = "bot@example.invalid"  # privacy-scan:allow (fake test git-config identity, not PII)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run([_GIT, "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return completed.stdout


def _write(repo: Path, path: str, body: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def refresh_repo(tmp_path: Path) -> Path:
    """A repo carrying ``main`` plus the two branches the gate has to tell apart."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", _FAKE_EMAIL)
    _git(repo, "config", "user.name", "refresh-bot")
    _git(repo, "config", "commit.gpgsign", "false")
    for path in _GENERATED:
        _write(repo, path, "before\n")
    _write(repo, "src/teatree/app.py", "VERSION = 1\n")
    _commit(repo, "base")

    _git(repo, "checkout", "-q", "-b", f"{_BRANCH_PREFIX}-2026-32")
    for path in _GENERATED:
        _write(repo, path, "after\n")
    _commit(repo, "chore(deps): weekly uv.lock upgrade")

    _git(repo, "checkout", "-q", "-b", f"{_BRANCH_PREFIX}-drifted", "main")
    for path in _GENERATED:
        _write(repo, path, "after\n")
    _write(repo, "src/teatree/app.py", "VERSION = 2\n")
    _write(repo, "tests/test_app.py", "def test_app() -> None:\n    assert True\n")
    _commit(repo, "chore(deps): weekly uv.lock upgrade")
    _git(repo, "checkout", "-q", "main")
    return repo


@pytest.mark.integration
class TestConstructedDiffs:
    """The load-bearing pair: two real branches differing only in which paths they touch."""

    @staticmethod
    def _scope(repo: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
        monkeypatch.chdir(repo)
        return main(list(args))

    def test_a_lock_only_refresh_still_auto_merges(
        self, refresh_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = tmp_path / "gh-output-clean"
        code = self._scope(
            refresh_repo,
            monkeypatch,
            "--base",
            "main",
            "--head",
            f"{_BRANCH_PREFIX}-2026-32",
            "--github-output",
            str(output),
            "--fail-on-escape",
        )
        assert code == 0
        assert "in-scope=true" in output.read_text(encoding="utf-8")

    def test_a_refresh_carrying_source_rewrites_does_not(
        self, refresh_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = tmp_path / "gh-output-drifted"
        comment = tmp_path / "hold.md"
        code = self._scope(
            refresh_repo,
            monkeypatch,
            "--base",
            "main",
            "--head",
            f"{_BRANCH_PREFIX}-drifted",
            "--github-output",
            str(output),
            "--comment-out",
            str(comment),
            "--fail-on-escape",
        )
        assert code == 1
        assert "in-scope=false" in output.read_text(encoding="utf-8")
        body = comment.read_text(encoding="utf-8")
        assert "`src/teatree/app.py`" in body
        assert "`tests/test_app.py`" in body

    def test_the_verdict_is_data_unless_the_caller_asks_for_a_gate(
        self, refresh_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = tmp_path / "gh-output-armtime"
        code = self._scope(
            refresh_repo,
            monkeypatch,
            "--base",
            "main",
            "--head",
            f"{_BRANCH_PREFIX}-drifted",
            "--github-output",
            str(output),
        )
        assert code == 0
        assert "in-scope=false" in output.read_text(encoding="utf-8")

    def test_an_unreadable_diff_is_held_not_read_as_empty(
        self, refresh_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = tmp_path / "gh-output-broken"
        code = self._scope(
            refresh_repo,
            monkeypatch,
            "--base",
            "main",
            "--head",
            "no-such-branch",
            "--github-output",
            str(output),
            "--fail-on-escape",
        )
        assert code == 1
        assert "in-scope=false" in output.read_text(encoding="utf-8")

    def test_runs_on_stdlib_alone(self, refresh_repo: Path) -> None:
        completed = subprocess.run(
            [sys.executable, "-S", str(_SCRIPT), "--base", "main", "--head", f"{_BRANCH_PREFIX}-2026-32"],
            capture_output=True,
            text=True,
            check=False,
            cwd=refresh_repo,
        )
        assert completed.returncode == 0, completed.stderr


class TestPathsFromMode:
    def test_a_forge_supplied_file_list_is_classified(self, tmp_path: Path) -> None:
        listing = tmp_path / "files.txt"
        listing.write_text("\n".join(_GENERATED) + "\n", encoding="utf-8")
        assert main(["--paths-from", str(listing), "--expected-count", "3"]) == 0

    def test_a_truncated_listing_is_held_before_any_path_is_judged(self, tmp_path: Path) -> None:
        listing = tmp_path / "files.txt"
        listing.write_text("\n".join(_GENERATED) + "\n", encoding="utf-8")
        assert main(["--paths-from", str(listing), "--expected-count", "191", "--fail-on-escape"]) == 1

    def test_stdin_is_accepted_as_the_listing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        listing = tmp_path / "piped.txt"
        listing.write_text("uv.lock\nsrc/teatree/app.py\n", encoding="utf-8")
        with listing.open(encoding="utf-8") as handle:
            monkeypatch.setattr(sys, "stdin", handle)
            assert main(["--paths-from", "-", "--expected-count", "2", "--fail-on-escape"]) == 1

    def test_an_unreadable_listing_is_held(self, tmp_path: Path) -> None:
        assert main(["--paths-from", str(tmp_path / "absent.txt"), "--expected-count", "3", "--fail-on-escape"]) == 1


class TestCliContract:
    @pytest.mark.parametrize(
        "argv",
        [
            [],
            ["--base", "main"],
            ["--paths-from", "-"],
            ["--paths-from", "-", "--expected-count", "1", "--base", "main", "--head", "HEAD"],
        ],
    )
    def test_an_ambiguous_or_incomplete_input_mode_is_refused(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(argv)
        assert excinfo.value.code == 2


def _workflow(path: Path) -> dict[Any, Any]:
    return cast("dict[Any, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML resolves a bare `on:` key to the boolean True.
    return cast("dict[str, Any]", workflow.get("on", workflow.get(True)))


def _refresh_steps() -> list[dict[str, Any]]:
    job = _workflow(_REFRESH_WORKFLOW)["jobs"]["refresh-lockfile"]
    return [step for step in job["steps"] if isinstance(step, dict)]


def _pr_step_run() -> str:
    return next(step for step in _refresh_steps() if step.get("name") == _PR_STEP)["run"]


def _scope_job_run() -> str:
    steps = _workflow(_SCOPE_WORKFLOW)["jobs"]["scope"]["steps"]
    return " ".join(step.get("run", "") for step in steps if isinstance(step, dict))


class TestArmTimeWiring:
    def test_the_scope_check_runs_before_auto_merge_is_armed(self) -> None:
        run = _pr_step_run()
        assert "scripts/ci/refresh_scope.py" in run, (
            "The arming path must measure the PR's own diff; the lock_delta verdict classifies "
            "versions and cannot see that 188 source files rode along."
        )
        assert run.index("refresh_scope.py") < run.index("--auto --squash")

    def test_arming_requires_both_the_version_verdict_and_the_scope_verdict(self) -> None:
        run = _pr_step_run()
        guard = run[: run.index("--auto --squash")]
        assert "AUTO_MERGE" in guard, "The #4437 patch-only verdict must still gate the arming."
        assert "SCOPE_OK" in guard, "The #4569 path-confinement verdict must gate it too."

    def test_a_scope_escape_clears_an_arming_a_previous_run_enabled(self) -> None:
        run = _pr_step_run()
        assert "--disable-auto" in run
        assert "4569" in run, "The withheld branch must name why, or it reads as the #4548 stall."


class TestMergeTimeWiring:
    def test_it_re_evaluates_on_every_push_not_only_at_creation(self) -> None:
        types = _triggers(_workflow(_SCOPE_WORKFLOW))["pull_request"]["types"]
        assert "synchronize" in types, (
            "Auto-merge is armed hours before it fires; without `synchronize` the gate would "
            "trust the diff as it stood at creation — exactly the #4569 failure."
        )

    def test_it_is_scoped_to_refresh_branches_so_ordinary_prs_pay_nothing(self) -> None:
        condition = _workflow(_SCOPE_WORKFLOW)["jobs"]["scope"]["if"]
        assert _BRANCH_PREFIX in condition
        assert "head_ref" in condition

    def test_it_can_disarm_and_comment(self) -> None:
        assert _workflow(_SCOPE_WORKFLOW)["permissions"]["pull-requests"] == "write"

    def test_the_disarm_precedes_the_comment_so_a_posting_failure_cannot_leave_it_armed(self) -> None:
        run = _scope_job_run()
        assert "--fail-on-escape" in run
        assert run.index("--disable-auto") < run.index(MARKER)

    def test_the_full_file_list_is_paginated_and_cross_checked(self) -> None:
        run = _scope_job_run()
        assert "--paginate" in run, "A 100-file page cap would hide escaping paths behind an in-scope prefix."
        assert "--expected-count" in run
