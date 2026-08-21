"""Tests for the weekly lock-refresh delta classifier and its workflow wiring (#4437).

The weekly ``uv-lock-upgrade`` workflow opens a PR with a PAT specifically so CI
fires, then calls ``gh pr merge --auto --squash``. Once ``test (3.13)`` goes
green there is no human step — so whatever the resolve moved, it lands. That is
right for a patch inside a pinned series and wrong for anything crossing a
feature or major boundary.

The classifier is the seam that tells those apart, so its FALSE verdicts are the
tests that matter: a minor move, a major move, and a version string it cannot
parse must all refuse auto-merge. A classifier that only ever says ``true``
guards nothing.

``scripts/ci/lock_delta.py`` imports no ``teatree``: the lock-refresh job runs
``uv lock --upgrade`` and no ``uv sync``, exactly the constraint
``scripts/ci/audit_canary.py`` documents, so reaching into the package would add
a full install to the job.
"""

import re
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from scripts.ci.lock_delta import Level, Move, auto_merge_safe, classify, compute_delta, main, parse_lock, render_body

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "uv-lock-upgrade.yml"
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "lock_delta.py"

_PR_STEP = "Open or update the lock-refresh PR"
_UPGRADE_RUN = "uv lock --upgrade"
_BRANCH_PREFIX = "chore/uv-lock-upgrade"


def _lock(*packages: tuple[str, str]) -> str:
    header = 'version = 1\nrequires-python = ">=3.13"\n'
    blocks = "".join(f'\n[[package]]\nname = "{name}"\nversion = "{version}"\n' for name, version in packages)
    return header + blocks


def _write_lock(path: Path, *packages: tuple[str, str]) -> Path:
    path.write_text(_lock(*packages), encoding="utf-8")
    return path


class TestClassify:
    @pytest.mark.parametrize(
        ("before", "after", "expected"),
        [
            ("1.2.3", "1.2.4", Level.PATCH),
            ("6.0.7", "6.0.8", Level.PATCH),
            ("1.2", "1.2.1", Level.PATCH),
            ("2.0.0b1", "2.0.0", Level.PATCH),
            ("1.2.4", "1.2.3", Level.PATCH),
            ("6.0.7", "6.1", Level.MINOR),
            ("0.9.1", "0.10.0", Level.MINOR),
            ("1.2.3", "2.0.0", Level.MAJOR),
            ("2.0.0", "1.9.0", Level.MAJOR),
        ],
    )
    def test_boundary_level(self, before: str, after: str, expected: Level) -> None:
        assert classify(before, after) is expected

    @pytest.mark.parametrize(("before", "after"), [("abc", "1.2.3"), ("1.2.3", ""), ("", "")])
    def test_unparsable_version_is_unknown_not_patch(self, before: str, after: str) -> None:
        assert classify(before, after) is Level.UNKNOWN


class TestComputeDelta:
    def test_reports_upgrades_additions_and_removals(self) -> None:
        before = parse_lock(_lock(("django", "6.0.7"), ("gone", "1.0.0")))
        after = parse_lock(_lock(("django", "6.0.8"), ("fresh", "2.0.0")))
        delta = compute_delta(before, after)
        assert delta.upgrades == (Move("django", "6.0.7", "6.0.8", Level.PATCH),)
        assert delta.added == (("fresh", "2.0.0"),)
        assert delta.removed == (("gone", "1.0.0"),)

    def test_unchanged_packages_are_not_moves(self) -> None:
        lock = parse_lock(_lock(("django", "6.0.7")))
        assert compute_delta(lock, lock).upgrades == ()

    def test_upgrades_are_ordered_most_severe_first(self) -> None:
        before = parse_lock(_lock(("a", "1.0.0"), ("b", "1.0.0"), ("c", "1.0.0")))
        after = parse_lock(_lock(("a", "1.0.1"), ("b", "2.0.0"), ("c", "1.1.0")))
        assert [move.name for move in compute_delta(before, after).upgrades] == ["b", "c", "a"]


class TestAutoMergeVerdict:
    def test_patch_only_resolve_is_safe(self) -> None:
        before = parse_lock(_lock(("django", "6.0.7")))
        after = parse_lock(_lock(("django", "6.0.8")))
        assert auto_merge_safe(compute_delta(before, after)) is True

    def test_a_feature_series_move_refuses_auto_merge(self) -> None:
        before = parse_lock(_lock(("django", "6.0.7")))
        after = parse_lock(_lock(("django", "6.1")))
        assert auto_merge_safe(compute_delta(before, after)) is False

    def test_a_major_move_refuses_auto_merge(self) -> None:
        before = parse_lock(_lock(("django", "6.0.7")))
        after = parse_lock(_lock(("django", "7.0.0")))
        assert auto_merge_safe(compute_delta(before, after)) is False

    def test_an_unparsable_version_refuses_auto_merge(self) -> None:
        before = parse_lock(_lock(("weird", "1.0.0")))
        after = parse_lock(_lock(("weird", "not-a-version")))
        assert auto_merge_safe(compute_delta(before, after)) is False

    def test_additions_and_removals_alone_stay_safe(self) -> None:
        before = parse_lock(_lock(("gone", "1.0.0")))
        after = parse_lock(_lock(("fresh", "2.0.0")))
        assert auto_merge_safe(compute_delta(before, after)) is True


class TestRenderBody:
    def test_names_every_move_and_the_enabled_verdict(self) -> None:
        before = parse_lock(_lock(("django", "6.0.7")))
        after = parse_lock(_lock(("django", "6.0.8")))
        body = render_body(compute_delta(before, after))
        assert "django" in body
        assert "6.0.7" in body
        assert "6.0.8" in body
        assert "auto-merge enabled" in body.lower()

    def test_names_the_boundary_crossing_package_when_review_is_required(self) -> None:
        before = parse_lock(_lock(("django", "6.0.7"), ("quiet", "1.0.0")))
        after = parse_lock(_lock(("django", "6.1"), ("quiet", "1.0.1")))
        body = render_body(compute_delta(before, after))
        assert "review required" in body.lower()
        assert "django" in body
        assert "quiet" in body

    def test_rows_stay_contiguous_so_github_renders_a_table(self) -> None:
        before = parse_lock(_lock(("django", "6.0.7"), ("httpx", "0.28.1")))
        after = parse_lock(_lock(("django", "6.0.8"), ("httpx", "0.28.2")))
        body = render_body(compute_delta(before, after))
        assert "| --- | --- | --- | --- |\n| `django` | 6.0.7 | 6.0.8 | patch |\n| `httpx`" in body, (
            "A blank line between rows renders as literal pipes, not a table."
        )

    def test_states_what_moved_even_when_nothing_crosses_a_boundary(self) -> None:
        before = parse_lock(_lock(("gone", "1.0.0")))
        after = parse_lock(_lock(("fresh", "2.0.0")))
        body = render_body(compute_delta(before, after))
        assert "fresh" in body
        assert "gone" in body


class TestMain:
    def test_writes_the_body_and_a_true_verdict(self, tmp_path: Path) -> None:
        before = _write_lock(tmp_path / "before.lock", ("django", "6.0.7"))
        after = _write_lock(tmp_path / "after.lock", ("django", "6.0.8"))
        body, output = tmp_path / "body.md", tmp_path / "gh-output"
        exit_code = main(
            [
                "--before",
                str(before),
                "--after",
                str(after),
                "--body-out",
                str(body),
                "--github-output",
                str(output),
            ]
        )
        assert exit_code == 0
        assert "6.0.8" in body.read_text(encoding="utf-8")
        assert "auto-merge=true" in output.read_text(encoding="utf-8")

    def test_writes_a_false_verdict_on_a_feature_move(self, tmp_path: Path) -> None:
        before = _write_lock(tmp_path / "before.lock", ("django", "6.0.7"))
        after = _write_lock(tmp_path / "after.lock", ("django", "6.1"))
        body, output = tmp_path / "body.md", tmp_path / "gh-output"
        assert (
            main(
                [
                    "--before",
                    str(before),
                    "--after",
                    str(after),
                    "--body-out",
                    str(body),
                    "--github-output",
                    str(output),
                ]
            )
            == 0
        )
        assert "auto-merge=false" in output.read_text(encoding="utf-8")

    def test_a_missing_lockfile_fails_loud_and_writes_no_verdict(self, tmp_path: Path) -> None:
        after = _write_lock(tmp_path / "after.lock", ("django", "6.0.8"))
        body, output = tmp_path / "body.md", tmp_path / "gh-output"
        exit_code = main(
            [
                "--before",
                str(tmp_path / "absent.lock"),
                "--after",
                str(after),
                "--body-out",
                str(body),
                "--github-output",
                str(output),
            ]
        )
        assert exit_code != 0
        assert not output.exists() or "auto-merge=true" not in output.read_text(encoding="utf-8")

    def test_an_unparsable_lockfile_fails_loud(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        before = _write_lock(tmp_path / "before.lock", ("django", "6.0.7"))
        after = tmp_path / "after.lock"
        after.write_text("this is not = = toml", encoding="utf-8")
        exit_code = main(
            [
                "--before",
                str(before),
                "--after",
                str(after),
                "--body-out",
                str(tmp_path / "body.md"),
                "--github-output",
                str(tmp_path / "gh-output"),
            ]
        )
        assert exit_code != 0
        assert "::error::" in capsys.readouterr().err

    def test_runs_on_stdlib_alone(self, tmp_path: Path) -> None:
        before = _write_lock(tmp_path / "before.lock", ("django", "6.0.7"))
        after = _write_lock(tmp_path / "after.lock", ("django", "6.0.8"))
        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                str(_SCRIPT),
                "--before",
                str(before),
                "--after",
                str(after),
                "--body-out",
                str(tmp_path / "body.md"),
                "--github-output",
                str(tmp_path / "gh-output"),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=tmp_path,
        )
        assert completed.returncode == 0, completed.stderr


def _job() -> dict[str, Any]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return cast("dict[str, Any]", workflow["jobs"]["refresh-lockfile"])


def _steps() -> list[dict[str, Any]]:
    return [step for step in _job()["steps"] if isinstance(step, dict)]


def _step_named(name: str) -> dict[str, Any]:
    return next(step for step in _steps() if step.get("name") == name)


def _index_of_run(fragment: str) -> int:
    return next(index for index, step in enumerate(_steps()) if fragment in step.get("run", ""))


class TestWorkflowWiring:
    def test_the_pre_upgrade_lockfile_is_captured_before_the_upgrade(self) -> None:
        assert _index_of_run("uv.lock.before") < _index_of_run(_UPGRADE_RUN), (
            "The before-lockfile must be copied BEFORE `uv lock --upgrade` overwrites it; "
            "a capture afterwards compares the new lockfile with itself and reports no moves."
        )

    def test_the_resolve_is_classified(self) -> None:
        runs = " ".join(step.get("run", "") for step in _steps())
        assert "scripts/ci/lock_delta.py" in runs

    def test_the_verdict_reaches_the_pr_step_as_an_env_var(self) -> None:
        delta_step = next(step for step in _steps() if "lock_delta.py" in step.get("run", ""))
        assert delta_step.get("id"), "The classifier step needs an id so its verdict output is addressable."
        env = _step_named(_PR_STEP).get("env", {})
        assert f"steps.{delta_step['id']}.outputs.auto-merge" in " ".join(str(value) for value in env.values()), (
            "The PR step must read the classifier's auto-merge verdict from its step output."
        )

    def test_auto_merge_is_gated_on_the_verdict(self) -> None:
        run = _step_named(_PR_STEP)["run"]
        match = re.search(r'if \[ "\$\{?AUTO_MERGE[^"]*" = "true" \]', run)
        assert match is not None, "`gh pr merge --auto` must sit behind an explicit patch-only guard."
        assert 0 < match.start() < run.find("--auto --squash"), (
            "Every `gh pr merge --auto --squash` must come AFTER the verdict guard — an "
            "unguarded one self-merges a feature-series migration over a weekend (#4437)."
        )

    def test_a_stale_auto_merge_is_disabled_when_review_is_required(self) -> None:
        assert "--disable-auto" in _step_named(_PR_STEP)["run"], (
            "The refresh reuses an open PR's branch, so a run whose resolve now crosses a "
            "boundary must clear auto-merge a previous patch-only run enabled."
        )

    def test_the_body_is_generated_not_hardcoded(self) -> None:
        run = _step_named(_PR_STEP)["run"]
        assert "--body-file" in run
        assert "PR_BODY=" not in run, (
            "A hardcoded body is what made #4432 claim an update its own lockfile did not "
            "produce; the body must be rendered from the measured delta."
        )

    def test_an_existing_pr_body_is_refreshed_not_left_stale(self) -> None:
        run = _step_named(_PR_STEP)["run"]
        assert "gh pr edit" in run, (
            "A rerun force-pushes a new resolve; without `gh pr edit --body-file` the PR body "
            "keeps describing the previous one."
        )

    def test_an_open_refresh_pr_is_reused_instead_of_accumulating(self) -> None:
        run = _step_named(_PR_STEP)["run"]
        reuse_hint = (
            "A review-required refresh does not merge, so the next weekly run must update that "
            "PR rather than open a second one every week."
        )
        assert "headRefName" in run, reuse_hint
        assert _BRANCH_PREFIX in run, reuse_hint
