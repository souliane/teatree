"""Fitness checks for the explicit eval lane names (souliane/teatree#1974).

The two deterministic eval lanes carry explicit, self-describing names:
``skill-triggers`` (skill-activation QA) and ``pinned-regressions`` (the
deterministic git/FSM corpus). Three contracts keep the rename a clean cutover:

-   both lanes resolve as registered ``t3 eval`` commands and the old lane
    names (``trigger-qa``, the lane-level ``eval regression`` command) are gone;
-   no tracked file still references an old lane name in a user-facing form
    (CLI command, lane label, prose) — the internal Python module/symbol names
    (``trigger_qa.py``, ``run_trigger_qa``, ``regression_corpus``) are NOT lane
    names and are explicitly excluded;
-   the deterministic lanes are wired into prek under their new names — the
    skill-triggers lane at the commit stage, the pinned-regressions lane at the
    push stage — and both prek entrypoints are invocable ``t3 eval`` commands.
"""

import re
import subprocess
from pathlib import Path

import yaml
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.eval import eval_app

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PRE_COMMIT_CONFIG = _REPO_ROOT / ".pre-commit-config.yaml"

# Old lane names in their user-facing surface forms. The internal module file
# (``trigger_qa.py``) and symbols (``run_trigger_qa``, ``regression_corpus``,
# ``RegressionReport``) are implementation, not lane names — they stay, mirroring
# the ``skill-coverage`` lane being backed by ``coverage.py``.
_STALE_LANE_FORMS = (
    re.compile(r"trigger-qa"),
    re.compile(r"\bt3 eval regression\b"),
    re.compile(r"`regression`\s+(?:checks|corpus|lane)", re.IGNORECASE),
)

# Tracked paths whose hits are the legitimate internal module/symbol names, not
# lane names — excluded from the no-stale scan.
_INTERNAL_NAME_PATHS = {
    "src/teatree/eval/trigger_qa.py",
    "src/teatree/eval/trigger_qa_corpus.yaml",
    "src/teatree/eval/regression_corpus.py",
    "src/teatree/eval/discovery.py",
    "tests/eval_replay/test_lane_names.py",
    # Asserts the OLD command name no longer resolves — the one legitimate
    # mention of the retired lane name.
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
    def test_new_lane_commands_are_registered(self) -> None:
        registered = _registered_eval_commands()
        assert "skill-triggers" in registered
        assert "pinned-regressions" in registered

    def test_old_lane_commands_are_gone(self) -> None:
        registered = _registered_eval_commands()
        assert "trigger-qa" not in registered
        assert "regression" not in registered

    def test_eval_all_table_uses_the_explicit_lane_labels(self) -> None:
        result = CliRunner().invoke(app, ["eval", "skill-triggers"])
        assert result.exit_code == 0, result.output


class TestNoStaleLaneName:
    def test_no_tracked_file_references_an_old_lane_name(self) -> None:
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


class TestPrekWiresDeterministicLanes:
    def _hooks_by_id(self) -> dict[str, dict]:
        config = yaml.safe_load(_PRE_COMMIT_CONFIG.read_text(encoding="utf-8"))
        hooks: dict[str, dict] = {}
        for repo in config["repos"]:
            for hook in repo.get("hooks", []):
                hooks[hook["id"]] = hook
        return hooks

    def test_skill_triggers_lane_runs_at_commit_stage(self) -> None:
        hooks = self._hooks_by_id()
        assert "eval-skill-triggers" in hooks, "skill-triggers prek hook missing"
        hook = hooks["eval-skill-triggers"]
        assert "skill-triggers" in hook["entry"]
        assert hook["stages"] == ["commit"], hook["stages"]

    def test_pinned_regressions_lane_runs_at_push_stage(self) -> None:
        hooks = self._hooks_by_id()
        assert "eval-pinned-regressions" in hooks, "pinned-regressions prek hook missing"
        hook = hooks["eval-pinned-regressions"]
        assert "pinned-regressions" in hook["entry"]
        assert hook["stages"] == ["push"], hook["stages"]

    def test_prek_entrypoints_are_invocable_eval_commands(self) -> None:
        registered = _registered_eval_commands()
        for hook_id in ("eval-skill-triggers", "eval-pinned-regressions"):
            hook = self._hooks_by_id()[hook_id]
            command = hook["entry"].split()[-1]
            assert command in registered, f"{hook_id} entry {command!r} is not a registered eval command"
