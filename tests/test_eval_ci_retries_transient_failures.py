"""The metered behavioral-eval workflow retries transient/flaky failures.

AI/trajectory evals are non-deterministic and reach the network/model API, so a
transient infra flake used to red-fail the pipeline and force a manual rerun.
The GitHub eval workflow wraps the eval and ``uv sync`` steps in
``nick-fields/retry``. These tests assert the attempt cap so an unbounded retry can never
mask a real regression.
"""

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GH_EVAL = _REPO_ROOT / ".github" / "workflows" / "eval.yml"

_RETRY_ACTION = "nick-fields/retry"
_MAX_RETRY_ATTEMPTS = 5


def _gh_eval_steps() -> list[dict[str, Any]]:
    jobs = cast("dict[str, Any]", yaml.safe_load(_GH_EVAL.read_text(encoding="utf-8"))["jobs"])
    assert "eval" in jobs, "the eval workflow must define the metered behavioral-eval job."
    return cast("list[dict[str, Any]]", jobs["eval"]["steps"])


def _step_using_retry_for(command_fragment: str) -> dict[str, Any]:
    for step in _gh_eval_steps():
        if step.get("uses", "").startswith(_RETRY_ACTION) and command_fragment in step.get("with", {}).get(
            "command", ""
        ):
            return step
    msg = f"No {_RETRY_ACTION} step wrapping a command containing {command_fragment!r} in the eval job."
    raise AssertionError(msg)


class TestGitHubEvalRetry:
    def test_behavioral_eval_step_is_retried(self) -> None:
        step = _step_using_retry_for("t3 eval run")
        attempts = int(step["with"]["max_attempts"])
        assert 2 <= attempts <= _MAX_RETRY_ATTEMPTS, (
            "Behavioral eval retry must be bounded (2-5 attempts) so a deterministic "
            f"eval miss still fails fast; got {attempts}."
        )

    def test_dependency_sync_is_retried(self) -> None:
        # `uv sync` is where Docker Hub/registry/PyPI ReadTimeouts hit.
        step = _step_using_retry_for("uv sync")
        assert 2 <= int(step["with"]["max_attempts"]) <= _MAX_RETRY_ATTEMPTS

    def test_retry_uses_backoff(self) -> None:
        step = _step_using_retry_for("t3 eval run")
        assert int(step["with"].get("retry_wait_seconds", 0)) > 0, (
            "Retry must wait between attempts so a transient outage has time to clear."
        )
