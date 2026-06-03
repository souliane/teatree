"""The weekly behavioral-eval CI job arms the all-skipped enforcement gate.

The eval runner SKIPs every scenario when ``claude`` is not on PATH / no
``ANTHROPIC_API_KEY``, and a fully-skipped suite reports green with zero
behavioral coverage. ``t3 eval run --require-executed`` turns that state red.
These tests pin that BOTH CI mirrors pass the flag (so a future workflow edit
that drops it is caught) and that it is armed only when a key is configured —
the no-key case legitimately has nothing the metered backend can execute.
"""

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GH_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_GITLAB_CI = _REPO_ROOT / ".gitlab-ci.yml"

_FLAG = "--require-executed"


def _gh_eval_run_command() -> str:
    jobs = cast("dict[str, Any]", yaml.safe_load(_GH_WORKFLOW.read_text(encoding="utf-8"))["jobs"])
    steps = cast("list[dict[str, Any]]", jobs["eval-weekly"]["steps"])
    for step in steps:
        command = step.get("with", {}).get("command", "")
        if "t3 eval run" in command:
            return command
    msg = "eval-weekly has no step running `t3 eval run`."
    raise AssertionError(msg)


def _gitlab_eval_script() -> list[str]:
    config = cast("dict[str, Any]", yaml.safe_load(_GITLAB_CI.read_text(encoding="utf-8")))
    return cast("list[str]", config["eval-weekly"]["script"])


class TestGitHubRequireExecuted:
    def test_eval_run_command_arms_the_flag(self) -> None:
        assert _FLAG in _gh_eval_run_command() or "require_executed" in _gh_eval_run_command(), (
            "The GitHub eval-weekly `t3 eval run` step must arm the all-skipped gate "
            "(directly or via a key-gated output) so a decorative run can't pass green."
        )

    def test_flag_is_armed_only_when_key_present(self) -> None:
        jobs = cast("dict[str, Any]", yaml.safe_load(_GH_WORKFLOW.read_text(encoding="utf-8"))["jobs"])
        text = yaml.safe_dump(jobs["eval-weekly"])
        assert "ANTHROPIC_API_KEY" in text, (
            "The gate must be keyed off ANTHROPIC_API_KEY so the no-key case (nothing "
            "the metered backend can run) is not turned into a permanent red."
        )


class TestGitLabRequireExecuted:
    def test_eval_run_line_arms_the_flag(self) -> None:
        joined = "\n".join(_gitlab_eval_script())
        assert _FLAG in joined, "The GitLab eval-weekly script must arm --require-executed on `t3 eval run`."

    def test_flag_is_armed_only_when_key_present(self) -> None:
        joined = "\n".join(_gitlab_eval_script())
        assert "ANTHROPIC_API_KEY" in joined, (
            "The GitLab gate must be keyed off ANTHROPIC_API_KEY so the no-key case stays a clean skip."
        )
