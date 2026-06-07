"""CI gates must FAIL LOUD when they cannot do their job.

A fake-green gate is one that exits 0 without doing its work: a scan over an
absent config, a swallowed error that skips the real run, a regeneration step
that never runs so the diff gate has nothing to catch. Each test here pins the
loud-on-failure shape of a CI YAML gate so a future edit that re-introduces the
skip-as-pass / fail-open swallow turns red HERE.

The script-level fail-loud fixes (banned-terms, blueprint-size, blueprint
cross-PR, doc-update, mutation-diff) have their RED tests beside their own
modules; this file covers the gates whose contract lives in the workflow YAML.
"""

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_PRECOMMIT_CONFIG = _REPO_ROOT / ".pre-commit-config.yaml"


def _load_ci_jobs() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"])


def _job_run_commands(job: dict[str, Any]) -> list[str]:
    return [step.get("run", "") for step in job.get("steps", []) if isinstance(step, dict)]


def _precommit_hooks() -> list[dict[str, Any]]:
    config = yaml.safe_load(_PRECOMMIT_CONFIG.read_text(encoding="utf-8"))
    hooks: list[dict[str, Any]] = []
    for repo in config["repos"]:
        hooks.extend(repo.get("hooks", []))
    return hooks


class TestSemgrepRegressionsJobIsGone:
    """Fix #1: the no-op advisory ``semgrep-regressions`` job is deleted.

    The job scanned ``.semgrep/warn`` — a directory that no longer exists (all
    rules promoted to ``.semgrep/blocking``). semgrep exits 0 on a missing
    config and the trailing ``|| true`` swallowed any non-zero, so the job was
    a no-op that always passed. The blocking set is enforced via prek; the
    advisory job loses no coverage. It must stay gone so a fake-green scan
    cannot silently reappear.
    """

    def test_no_semgrep_regressions_ci_job(self) -> None:
        assert "semgrep-regressions" not in _load_ci_jobs(), (
            "The advisory semgrep-regressions CI job scanned the now-absent "
            ".semgrep/warn and always exited 0 (fake-green). It must stay deleted; "
            "the blocking set is enforced via the prek step in the lint job."
        )

    def test_no_ci_step_scans_the_absent_warn_dir(self) -> None:
        joined = " ".join(cmd for job in _load_ci_jobs().values() for cmd in _job_run_commands(job))
        assert ".semgrep/warn" not in joined, (
            "No CI step may scan .semgrep/warn — the directory does not exist, so "
            "the scan is a no-op that exits 0 (fake-green)."
        )

    def test_blocking_set_is_enforced_via_prek(self) -> None:
        # Deleting the advisory job loses no coverage only because the blocking
        # set rides the prek step with --error (fails CI on a finding).
        blocking_hooks = [
            hook
            for hook in _precommit_hooks()
            if hook.get("alias") == "semgrep-regressions"
            and ".semgrep/blocking" in " ".join(str(a) for a in hook.get("args", []))
        ]
        assert blocking_hooks, "The blocking regression set must be enforced via a prek semgrep-regressions hook."
        for hook in blocking_hooks:
            args = " ".join(str(a) for a in hook.get("args", []))
            assert "--error" in args, "The blocking semgrep hook must pass --error so a finding fails CI."


class TestMutationFullGateRunsWhenUncertain:
    """Fix #2: the mutation-full weekly gate fails SAFE, not silent-skip.

    The gate fetched the PR list with ``gh api ... > prs.json || echo '[]'``.
    On any gh failure the empty list made ``first_pr_of_week.py`` return False,
    so the gate wrote ``run_mutation=false`` and skipped — exiting 0 even when
    it should have run. The fix drops the silent ``|| echo "[]"`` swallow and
    defaults to RUNNING when prs.json can't be fetched (run-when-uncertain).
    """

    def _gate_step_run(self) -> str:
        gate_steps = [
            step
            for step in _load_ci_jobs()["mutation-full"]["steps"]
            if isinstance(step, dict) and step.get("id") == "gate"
        ]
        assert gate_steps, "mutation-full must have a step with id 'gate'."
        return gate_steps[0]["run"]

    def test_no_silent_empty_list_fallback(self) -> None:
        run = self._gate_step_run()
        swallow = "[]"
        fallback_present = f'echo "{swallow}"' in run or f"echo '{swallow}'" in run
        assert not fallback_present, (
            "mutation-full must not swallow a gh failure into an empty PR list — that "
            "silently skips the run (fake-green). On fetch failure, default to running."
        )

    def test_defaults_to_running_when_pr_list_unavailable(self) -> None:
        run = self._gate_step_run()
        assert "run_mutation=true" in run, (
            "mutation-full's gate must set run_mutation=true when the PR list cannot be "
            "fetched (run-when-uncertain), not skip the mutation run."
        )


class TestDocsDriftRegeneratesAntipatternCatalog:
    """Fix #5: docs-drift regenerates the antipattern catalog before diffing.

    ``docs/generated/antipattern-catalog.md`` was tracked + in the mkdocs nav
    but never regenerated in CI, so ``git diff --exit-code docs/generated``
    could not catch drift between ``antipatterns.yaml`` and the committed
    catalog. The fix adds the generator step BEFORE the diff assertion, mirroring
    how generate_cli_reference.py is invoked.
    """

    def _docs_drift_steps(self) -> list[dict[str, Any]]:
        return [s for s in _load_ci_jobs()["docs-drift"]["steps"] if isinstance(s, dict)]

    def test_catalog_generator_runs_in_docs_drift(self) -> None:
        joined = " ".join(_job_run_commands(_load_ci_jobs()["docs-drift"]))
        assert "generate_antipattern_catalog.py" in joined, (
            "docs-drift must regenerate the antipattern catalog so the docs/generated "
            "diff gate can catch antipatterns.yaml -> catalog drift."
        )

    def test_catalog_generator_runs_before_the_diff_assertion(self) -> None:
        runs = _job_run_commands(_load_ci_jobs()["docs-drift"])
        gen_idx = next(i for i, r in enumerate(runs) if "generate_antipattern_catalog.py" in r)
        diff_idx = next(i for i, r in enumerate(runs) if "git diff --exit-code docs/generated" in r)
        assert gen_idx < diff_idx, (
            "The antipattern catalog generator must run BEFORE the "
            "`git diff --exit-code docs/generated` assertion, else the diff gate has "
            "nothing to catch."
        )

    def test_catalog_generator_does_not_self_stage(self) -> None:
        # The generator auto-stages on change; in CI that would make the working
        # tree match the index and hide drift from `git diff` (no --cached). The
        # step must disable staging via the env var so the diff gate stays loud.
        gen_steps = [s for s in self._docs_drift_steps() if "generate_antipattern_catalog.py" in str(s.get("run", ""))]
        assert gen_steps, "docs-drift must have a step that runs the antipattern catalog generator."
        env = gen_steps[0].get("env", {})
        assert str(env.get("ANTIPATTERN_CATALOG_NO_STAGE", "")) == "1", (
            "The catalog generator step must set ANTIPATTERN_CATALOG_NO_STAGE=1 so it does "
            "not git-add the regenerated file (which would hide drift from git diff)."
        )
