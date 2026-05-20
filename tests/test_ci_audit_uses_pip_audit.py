"""Tests that CI's dependency-audit gate uses ``pip-audit`` (#1264).

The flaky ``uv audit --preview-features audit`` preview command is not
allowed in CI or the pre-commit hook anymore.

`uv audit` is still preview-state. Upstream regression
[astral-sh/uv#19492](https://github.com/astral-sh/uv/issues/19492)
caused every teatree PR's audit job to error out with
``error decoding response body / expected value at line 1 column ...``
when the OSV vulnerability database returned an empty-events range
record. Upstream chose to keep strict OSV parsing, so the audit gate
needs a production-stable tool. ``pip-audit`` (pypa/pip-audit) uses the
same OSV backend, is established, and is invoked via ``uvx`` so no
environment install step is needed.

These tests pin the workflow + pre-commit hook to ``pip-audit`` so a
future regression can't silently restore the broken command.
"""

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_PRECOMMIT_CONFIG = _REPO_ROOT / ".pre-commit-config.yaml"


def _load_ci_jobs() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"])


def _load_precommit_hooks() -> list[dict[str, Any]]:
    config = yaml.safe_load(_PRECOMMIT_CONFIG.read_text(encoding="utf-8"))
    hooks: list[dict[str, Any]] = []
    for repo in config["repos"]:
        hooks.extend(repo.get("hooks", []))
    return hooks


class TestCiAuditJob:
    def test_uv_audit_job_exists(self) -> None:
        # The job name stays ``uv-audit`` so the PR-sweep scanner's
        # ``--fallback-uv-audit`` escape hatch
        # (``src/teatree/loop/scanners/pr_sweep.py``,
        # ``UV_AUDIT_CHECK_NAME``) keeps matching the check name.
        jobs = _load_ci_jobs()
        assert "uv-audit" in jobs, (
            "The CI job must be named 'uv-audit' to remain compatible "
            "with the PR-sweep scanner's fallback escape hatch."
        )

    def test_audit_job_does_not_call_broken_uv_audit(self) -> None:
        # Upstream parser bug astral-sh/uv#19492 — never restore.
        jobs = _load_ci_jobs()
        steps = jobs["uv-audit"]["steps"]
        commands = [step.get("run", "") for step in steps if isinstance(step, dict)]
        joined = " ".join(commands)
        assert "uv audit" not in joined, (
            "CI must not call 'uv audit'; the preview feature is unstable "
            "(astral-sh/uv#19492). Use 'pip-audit' instead."
        )

    def test_audit_job_uses_pip_audit(self) -> None:
        jobs = _load_ci_jobs()
        steps = jobs["uv-audit"]["steps"]
        commands = [step.get("run", "") for step in steps if isinstance(step, dict)]
        joined = " ".join(commands)
        assert "pip-audit" in joined, "CI's uv-audit job must invoke 'pip-audit' as the dependency audit tool (#1264)."


class TestPrecommitAuditHook:
    def test_audit_hook_does_not_call_broken_uv_audit(self) -> None:
        hooks = _load_precommit_hooks()
        audit_hooks = [hook for hook in hooks if hook.get("id") == "uv-audit"]
        assert audit_hooks, "Expected a hook with id 'uv-audit' in .pre-commit-config.yaml."
        for hook in audit_hooks:
            entry = str(hook.get("entry", ""))
            assert "uv audit" not in entry, (
                "Pre-commit hook 'uv-audit' must not call 'uv audit'; use 'pip-audit' instead (#1264)."
            )

    def test_audit_hook_uses_pip_audit(self) -> None:
        hooks = _load_precommit_hooks()
        audit_hooks = [hook for hook in hooks if hook.get("id") == "uv-audit"]
        assert audit_hooks
        for hook in audit_hooks:
            entry = str(hook.get("entry", ""))
            assert "pip-audit" in entry, "Pre-commit hook 'uv-audit' must invoke 'pip-audit' (#1264)."
