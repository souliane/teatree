import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests._actions_workflow import CI_WEEKLY_CRON, github_context, job_results, load, ran
from tests._git_repo import make_git_repo, run_git

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_ENTRY = "scripts/hooks/refuse-tracked-ignored-files.sh"
_HOOK = _REPO_ROOT / _HOOK_ENTRY


def _run_hook(cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(
        [str(_HOOK)],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _repo_tracking(tmp_path: Path, *, gitignore: str, path: str, force: bool) -> Path:
    repo = make_git_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text(gitignore, encoding="utf-8")
    tracked = repo / path
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("content\n", encoding="utf-8")
    run_git(repo, "add", ".gitignore")
    run_git(repo, "add", *(["-f"] if force else []), path)
    return repo


def test_refuses_a_force_added_ignored_file(tmp_path: Path) -> None:
    repo = _repo_tracking(tmp_path, gitignore="docs/plans/\n", path="docs/plans/design.md", force=True)

    result = _run_hook(repo)

    assert result.returncode == 1
    assert "docs/plans/design.md" in result.stderr


def test_passes_once_the_ignored_file_is_untracked(tmp_path: Path) -> None:
    repo = _repo_tracking(tmp_path, gitignore="docs/plans/\n", path="docs/plans/design.md", force=True)
    run_git(repo, "rm", "-q", "--cached", "docs/plans/design.md")

    result = _run_hook(repo)

    assert result.returncode == 0, result.stderr


def test_passes_a_file_re_included_by_a_negation(tmp_path: Path) -> None:
    repo = _repo_tracking(
        tmp_path,
        gitignore="docs/design/*\n!docs/design/decision.md\n",
        path="docs/design/decision.md",
        force=False,
    )

    result = _run_hook(repo)

    assert result.returncode == 0, result.stderr


def test_a_personal_exclude_rule_does_not_refuse(tmp_path: Path) -> None:
    repo = _repo_tracking(tmp_path, gitignore="", path="notes.md", force=False)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("notes.md\n", encoding="utf-8")

    result = _run_hook(repo)

    assert result.returncode == 0, result.stderr


def test_the_repos_own_tree_passes() -> None:
    result = _run_hook(_REPO_ROOT)

    assert result.returncode == 0, result.stderr


def test_every_commit_runs_the_guard() -> None:
    config = yaml.safe_load((_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [hook for repo in config["repos"] for hook in repo.get("hooks", [])]

    assert any(hook.get("entry") == _HOOK_ENTRY and "commit" in hook.get("stages", []) for hook in hooks)


def test_the_ci_job_runs_the_guard_with_a_timeout() -> None:
    job = load()["jobs"]["tracked-ignored-files"]

    assert job["timeout-minutes"] > 0
    assert any(step.get("run") == _HOOK_ENTRY for step in job["steps"])


@pytest.mark.parametrize(
    "github",
    [
        github_context("pull_request", ref="refs/pull/42/merge"),
        github_context("push"),
        github_context("schedule", schedule=CI_WEEKLY_CRON),
    ],
    ids=["pull-request", "push-to-main", "weekly-cron"],
)
def test_every_pr_push_to_main_and_weekly_run_runs_the_guard_in_ci(github: dict[str, Any]) -> None:
    assert "tracked-ignored-files" in ran(job_results(load(), github))
