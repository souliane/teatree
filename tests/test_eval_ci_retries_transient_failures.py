"""The metered behavioral-eval workflow retries transient/flaky failures.

AI/trajectory evals are non-deterministic and reach the network/model API, so a
transient infra flake used to red-fail the pipeline and force a manual rerun.
The GitHub eval workflow wraps the eval and ``uv sync`` steps in
``nick-fields/retry`` and the GitLab eval-suite carries a bounded ``retry:``.
These tests pin both, and assert the attempt cap so an unbounded retry can never
mask a real regression.
"""

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GH_EVAL = _REPO_ROOT / ".github" / "workflows" / "eval.yml"
_GITLAB_CI = _REPO_ROOT / ".gitlab-ci.yml"

_RETRY_ACTION = "nick-fields/retry"
_MAX_RETRY_ATTEMPTS = 5


def _gh_eval_steps() -> list[dict[str, Any]]:
    jobs = cast("dict[str, Any]", yaml.safe_load(_GH_EVAL.read_text(encoding="utf-8"))["jobs"])
    assert "eval" in jobs, "the eval workflow must define the metered behavioral-eval job."
    return cast("list[dict[str, Any]]", jobs["eval"]["steps"])


def _gitlab_eval_suite() -> dict[str, Any]:
    config = cast("dict[str, Any]", yaml.safe_load(_GITLAB_CI.read_text(encoding="utf-8")))
    assert ".eval-suite" in config, "GitLab CI must define the shared eval-suite template."
    return cast("dict[str, Any]", config[".eval-suite"])


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


class TestGitLabEvalRetry:
    def test_eval_job_defines_bounded_retry(self) -> None:
        retry = _gitlab_eval_suite().get("retry")
        assert retry is not None, "GitLab eval-suite must declare a retry policy for transient flakes."
        assert isinstance(retry, dict), "retry must specify max + when (not a bare integer that retries on anything)."
        assert 1 <= int(retry["max"]) <= _MAX_RETRY_ATTEMPTS, (
            "GitLab eval retry must be bounded so a deterministic miss fails fast."
        )

    def test_retry_targets_transient_failure_classes(self) -> None:
        when = set(_gitlab_eval_suite()["retry"]["when"])
        # Retrying on transient signals, not a blanket `always` that masks bugs.
        assert "always" not in when, "retry must not be 'always' — that masks deterministic eval regressions."
        assert {"runner_system_failure", "stuck_or_timeout_failure"} <= when, (
            "retry must cover the runner/system + timeout transient classes."
        )
