"""Fitness checks for the explicit eval lane names (souliane/teatree#1974, #1189).

The free-text ``skill-triggers`` lane was removed with explicit skill loading;
``pinned-regressions`` (the deterministic git/FSM corpus) is the surviving
deterministic prek lane. Three contracts keep the cutover clean:

-   ``pinned-regressions`` resolves as a registered ``t3 eval`` command and the
    retired lane names (``skill-triggers``, ``trigger-qa``, the lane-level
    ``eval regression`` command) are gone;
-   no tracked file still references a retired lane name in a user-facing form
    (CLI command, lane label, prose);
-   the surviving deterministic lane is wired into prek under its name at the
    push stage, and that entrypoint is an invocable ``t3 eval`` command.
"""

import re
import subprocess
from pathlib import Path

import yaml

from teatree.cli.eval import eval_app

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PRE_COMMIT_CONFIG = _REPO_ROOT / ".pre-commit-config.yaml"

# Retired lane names in their user-facing surface forms.
_STALE_LANE_FORMS = (
    re.compile(r"trigger-qa"),
    re.compile(r"eval skill-triggers"),
    re.compile(r"eval-skill-triggers"),
    re.compile(r"\bt3 eval regression\b"),
    re.compile(r"`regression`\s+(?:checks|corpus|lane)", re.IGNORECASE),
)

# Tracked paths whose hits are legitimate — the one test asserting the retired
# command no longer resolves, plus this file's own pattern list.
_INTERNAL_NAME_PATHS = {
    "tests/eval_replay/test_lane_names.py",
    "tests/teatree_cli/test_eval.py",
}


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"],  # noqa: S607
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [_REPO_ROOT / line for line in out.stdout.splitlines() if line]


def _registered_eval_commands() -> set[str]:
    return {command.name for command in eval_app.registered_commands if command.name}


class TestLaneNamesResolve:
    def test_pinned_regressions_command_is_registered(self) -> None:
        assert "pinned-regressions" in _registered_eval_commands()

    def test_retired_lane_commands_are_gone(self) -> None:
        registered = _registered_eval_commands()
        assert "skill-triggers" not in registered
        assert "trigger-qa" not in registered
        assert "regression" not in registered


class TestNoStaleLaneName:
    def test_no_tracked_file_references_a_retired_lane_name(self) -> None:
        offenders: list[str] = []
        for path in _tracked_text_files():
            rel = path.relative_to(_REPO_ROOT).as_posix()
            if rel in _INTERNAL_NAME_PATHS:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, FileNotFoundError):
                continue
            for pattern in _STALE_LANE_FORMS:
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    offenders.append(f"{rel}:{line}: {match.group(0)!r}")
        assert not offenders, "stale eval lane name(s) found:\n" + "\n".join(offenders)


class TestPrekWiresDeterministicLane:
    def _hooks_by_id(self) -> dict[str, dict]:
        config = yaml.safe_load(_PRE_COMMIT_CONFIG.read_text(encoding="utf-8"))
        hooks: dict[str, dict] = {}
        for repo in config["repos"]:
            for hook in repo.get("hooks", []):
                hooks[hook["id"]] = hook
        return hooks

    def test_skill_triggers_hook_is_gone(self) -> None:
        assert "eval-skill-triggers" not in self._hooks_by_id()

    def test_pinned_regressions_lane_runs_at_push_stage(self) -> None:
        hooks = self._hooks_by_id()
        assert "eval-pinned-regressions" in hooks, "pinned-regressions prek hook missing"
        hook = hooks["eval-pinned-regressions"]
        assert "pinned-regressions" in hook["entry"]
        assert hook["stages"] == ["push"], hook["stages"]

    def test_prek_entrypoint_is_an_invocable_eval_command(self) -> None:
        registered = _registered_eval_commands()
        hook = self._hooks_by_id()["eval-pinned-regressions"]
        command = hook["entry"].split()[-1]
        assert command in registered, f"eval-pinned-regressions entry {command!r} is not a registered eval command"
