"""A lock refresh self-merges only while its branch changes nothing beyond lock/generated paths (#4569)."""

import dataclasses
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from scripts.ci.refresh_scope import main
from tests._actions_workflow import evaluate, triggers
from tests._git_repo import run_git as _git

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "refresh_scope.py"
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_BASH = shutil.which("bash") or "/bin/bash"
_PR = "7"

_BASE_FILES = {
    "uv.lock": "version = 1\n",
    "dist/sbom.json": "{}\n",
    "docs/generated/cli-reference.md": "# CLI\n",
    "src/teatree/app.py": "VALUE = 1\n",
    "tests/test_app.py": "def test_value() -> None: ...\n",
}
_BRANCHES: dict[str, dict[str, str]] = {
    "lock-only": {"uv.lock": "version = 2\n", "dist/sbom.json": '{"v": 2}\n', "docs/generated/cli-reference.md": "#\n"},
    "source": {"uv.lock": "version = 2\n", "src/teatree/app.py": "VALUE = 2\n", "tests/test_app.py": "# moved\n"},
}


@dataclasses.dataclass(frozen=True)
class RefreshRepo:
    path: Path
    base: str
    heads: dict[str, str]


def _write(repo: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _commit_branch(repo: Path, name: str, change: dict[str, str] | None = None) -> str:
    _git(repo, "checkout", "-q", "-b", name, "main")
    if change is None:
        _git(repo, "mv", "src/teatree/app.py", "docs/generated/app.py")
    else:
        _write(repo, change)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", name)
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return head


@pytest.fixture
def refresh_repo(tmp_path: Path) -> RefreshRepo:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, {**_BASE_FILES, "scripts/ci/refresh_scope.py": _SCRIPT.read_text(encoding="utf-8")})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    heads = {name: _commit_branch(repo, name, change) for name, change in _BRANCHES.items()}
    heads["rename-into-generated"] = _commit_branch(repo, "rename-into-generated")
    return RefreshRepo(path=repo, base=_git(repo, "rev-parse", "main"), heads=heads)


class TestScopeVerdict:
    @pytest.mark.parametrize(
        ("branch", "expected"),
        [("lock-only", 0), ("source", 1), ("rename-into-generated", 1)],
    )
    def test_only_a_lock_and_generated_diff_is_in_scope(
        self, refresh_repo: RefreshRepo, monkeypatch: pytest.MonkeyPatch, branch: str, expected: int
    ) -> None:
        monkeypatch.chdir(refresh_repo.path)
        assert main(["--base", "main", "--head", branch]) == expected

    def test_an_escape_names_every_path_beyond_the_allowlist(
        self, refresh_repo: RefreshRepo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(refresh_repo.path)
        main(["--base", "main", "--head", "source"])
        out = capsys.readouterr().out
        assert "src/teatree/app.py" in out
        assert "tests/test_app.py" in out
        assert "uv.lock" not in out

    @pytest.mark.parametrize(("base", "head"), [("no-such-ref", "source"), ("main", "main")])
    def test_an_unreadable_or_empty_diff_cannot_vouch_for_a_merge(
        self,
        refresh_repo: RefreshRepo,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        base: str,
        head: str,
    ) -> None:
        monkeypatch.chdir(refresh_repo.path)
        assert main(["--base", base, "--head", head]) == 2
        assert "::error::" in capsys.readouterr().err

    def test_runs_on_stdlib_alone(self, refresh_repo: RefreshRepo) -> None:
        completed = subprocess.run(
            [sys.executable, "-S", str(_SCRIPT), "--base", "main", "--head", "lock-only"],
            capture_output=True,
            text=True,
            check=False,
            cwd=refresh_repo.path,
        )
        assert completed.returncode == 0, completed.stderr


def _pr_step_run() -> str:
    workflow = yaml.safe_load((_WORKFLOWS / "uv-lock-upgrade.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["refresh-lockfile"]["steps"]
    return next(str(step["run"]) for step in steps if step.get("name") == "Open or update the lock-refresh PR")


class TestArmTimeGate:
    def test_auto_merge_arms_only_for_an_in_scope_patch_level_refresh(self) -> None:
        run = _pr_step_run()
        guard = re.search(r'if \[ "\$\{?AUTO_MERGE[^"]*" = "true" \] && \[ "\$\{?SCOPE_OK[^"]*" = "true" \]', run)
        assert guard is not None, "`gh pr merge --auto` must require the path-scope verdict as well."
        assert run.index("scripts/ci/refresh_scope.py") < guard.start() < run.index("--auto --squash")

    def test_a_withheld_refresh_is_routed_to_the_factory_cold_review_never_a_human(self) -> None:
        withheld = _pr_step_run().split("--auto --squash", 1)[1]
        assert "::warning::" in withheld
        assert "it goes to the factory's cold review, then merges" in withheld
        assert "human" not in (_WORKFLOWS / "uv-lock-upgrade.yml").read_text(encoding="utf-8")


def _scope_workflow() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load((_WORKFLOWS / "lock-refresh-scope.yml").read_text(encoding="utf-8")))


def _starts_with(text: object, prefix: object) -> bool:
    return str(text).lower().startswith(str(prefix).lower())


class TestMergeTimeTrigger:
    def test_every_push_to_a_refresh_pr_is_judged_by_the_base_workflow(self) -> None:
        on = triggers(_scope_workflow())
        assert "pull_request" not in on, "a PR-defined workflow could rewrite its own judge"
        assert "synchronize" in on["pull_request_target"]["types"]

    @pytest.mark.parametrize(("head_ref", "runs"), [("chore/uv-lock-upgrade-2026-40", True), ("feature/x", False)])
    def test_only_refresh_branches_run_the_check(self, head_ref: str, *, runs: bool) -> None:
        condition = str(_scope_workflow()["jobs"]["scope"]["if"])
        context = {"github": {"head_ref": head_ref}}
        assert evaluate(condition, context, {"startsWith": _starts_with}) is runs


def _scope_step_run() -> str:
    steps = _scope_workflow()["jobs"]["scope"]["steps"]
    return next(str(step["run"]) for step in steps if "refresh_scope.py" in str(step.get("run", "")))


_GH_STUB = """#!/bin/sh
echo "$*" >> "$GH_LOG"
case "$*" in
    "pr view"*) cat "$GH_STATE" ;;
    "pr merge"*--disable-auto*) [ -n "$GH_DISARM_FAILS" ] && exit 1; echo false > "$GH_STATE" ;;
esac
"""


def _run_scope_step(
    refresh_repo: RefreshRepo, tmp_path: Path, head: str, *, disarm_fails: bool = False
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(refresh_repo.path, "remote", "add", "origin", str(origin))
    pushed = head if head in refresh_repo.heads else "source"
    _git(refresh_repo.path, "push", "-q", "origin", "main", f"{pushed}:refs/pull/{_PR}/head")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {"gh": _GH_STUB, "python": f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'}.items():
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    log, state = tmp_path / "gh.log", tmp_path / "gh.state"
    log.touch()
    state.write_text("true\n", encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "PR": _PR,
        "BASE_SHA": refresh_repo.base,
        "HEAD_SHA": refresh_repo.heads.get(head, "f" * 40),
        "GH_LOG": str(log),
        "GH_STATE": str(state),
        "GH_DISARM_FAILS": "1" if disarm_fails else "",
    }
    completed = subprocess.run(
        [_BASH, "-e", "-c", _scope_step_run()],
        capture_output=True,
        text=True,
        env=env,
        cwd=refresh_repo.path,
        check=False,
    )
    return completed, log.read_text(encoding="utf-8"), state.read_text(encoding="utf-8").strip()


class TestMergeTimeDisarm:
    def test_a_lock_only_push_leaves_auto_merge_armed(self, refresh_repo: RefreshRepo, tmp_path: Path) -> None:
        completed, gh_calls, armed = _run_scope_step(refresh_repo, tmp_path, "lock-only")
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "--disable-auto" not in gh_calls
        assert armed == "true"

    def test_source_pushed_onto_the_branch_disarms_and_routes_to_cold_review_without_red(
        self, refresh_repo: RefreshRepo, tmp_path: Path
    ) -> None:
        completed, gh_calls, armed = _run_scope_step(refresh_repo, tmp_path, "source")
        assert f"pr merge {_PR} --disable-auto" in gh_calls
        assert armed == "false"
        assert completed.returncode == 0, "an out-of-scope bump is reviewed and merged, never refused by a red check"
        assert "it goes to the factory's cold review, then merges" in completed.stdout

    def test_a_disarm_that_does_not_land_fails_loud(self, refresh_repo: RefreshRepo, tmp_path: Path) -> None:
        completed, _, armed = _run_scope_step(refresh_repo, tmp_path, "source", disarm_fails=True)
        assert armed == "true"
        assert completed.returncode != 0
        assert "::error::" in completed.stdout

    def test_an_unreadable_head_disarms_and_fails_loud(self, refresh_repo: RefreshRepo, tmp_path: Path) -> None:
        completed, gh_calls, armed = _run_scope_step(refresh_repo, tmp_path, "vanished")
        assert "--disable-auto" in gh_calls
        assert armed == "false"
        assert completed.returncode != 0
