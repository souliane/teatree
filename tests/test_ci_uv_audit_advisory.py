"""Tests for the advisory ``uv audit`` CI lane (souliane/teatree#2292).

``uv audit`` (Astral, https://astral.sh/blog/uv-audit) is a uv-native,
``uv.lock``-accurate, OSV-backed vulnerability scanner. It is still a PREVIEW
feature with possible breaking changes, so it runs as a SEPARATE, NON-BLOCKING
advisory lane — never the blocking gate. The blocking gate stays ``pip-audit``
(the ``uv-audit`` job, #1264) and the SBOM-diff gate (``sbom``) is untouched:
``uv audit`` runs ALONGSIDE both, it does not replace them.

These tests pin:
- the advisory job is DISTINCT from the load-bearing ``uv-audit`` job key (UV_AUDIT_CHECK_NAME in pr_sweep.py);
- it actually invokes ``uv audit``;
- it is NON-BLOCKING (a finding never reds the build);
- the uv version is PINNED so preview flag churn can't break the lane;
- the existing blocking pip-audit gate and SBOM-diff gate still exist.
"""

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

_ADVISORY_JOB = "uv-audit-advisory"
_BLOCKING_JOB = "uv-audit"


def _load_ci_jobs() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"])


def _job_run_commands(job: dict[str, Any]) -> list[str]:
    return [step.get("run", "") for step in job["steps"] if isinstance(step, dict)]


class TestUvAuditAdvisoryJob:
    def test_advisory_job_exists_and_is_distinct_from_blocking_gate(self) -> None:
        jobs = _load_ci_jobs()
        assert _ADVISORY_JOB in jobs, f"Expected a distinct advisory job '{_ADVISORY_JOB}' running 'uv audit'."
        assert _ADVISORY_JOB != _BLOCKING_JOB, (
            "The advisory lane must not reuse the load-bearing 'uv-audit' job key "
            "(UV_AUDIT_CHECK_NAME in src/teatree/loop/scanners/pr_sweep.py)."
        )

    def test_advisory_job_invokes_uv_audit(self) -> None:
        jobs = _load_ci_jobs()
        joined = " ".join(_job_run_commands(jobs[_ADVISORY_JOB]))
        assert "uv audit" in joined, "The advisory lane must invoke 'uv audit'."

    def test_advisory_job_reads_the_lockfile_frozen(self) -> None:
        # ``--frozen`` audits the locked ``uv.lock`` resolution without
        # re-locking — the lockfile-accurate scan the ticket asks for.
        jobs = _load_ci_jobs()
        joined = " ".join(_job_run_commands(jobs[_ADVISORY_JOB]))
        assert "--frozen" in joined, "The advisory lane must audit the locked 'uv.lock' resolution (--frozen)."

    def test_advisory_job_is_non_blocking(self) -> None:
        # Non-blocking has two independent guards so a single mistake can't
        # silently turn the advisory lane into a gate:
        #  - the audit step carries ``continue-on-error: true``;
        #  - the command tolerates a non-zero exit (``|| true``).
        jobs = _load_ci_jobs()
        steps = [s for s in jobs[_ADVISORY_JOB]["steps"] if isinstance(s, dict)]
        audit_steps = [s for s in steps if "uv audit" in s.get("run", "")]
        assert audit_steps, "Expected a step that runs 'uv audit'."
        for step in audit_steps:
            assert step.get("continue-on-error") is True, (
                "The 'uv audit' step must set continue-on-error: true so a "
                "finding (or preview/network hiccup) never reds the build."
            )
            assert "|| true" in step["run"], (
                "The 'uv audit' command must tolerate a non-zero exit (|| true) "
                "so preview instability never fails the lane."
            )

    def test_advisory_job_pins_uv_version(self) -> None:
        # setup-uv must pin a fixed ``version:`` so a future uv release with
        # preview flag churn can't break the advisory lane out from under us.
        jobs = _load_ci_jobs()
        setup_uv_steps = [
            s
            for s in jobs[_ADVISORY_JOB]["steps"]
            if isinstance(s, dict) and "astral-sh/setup-uv" in str(s.get("uses", ""))
        ]
        assert setup_uv_steps, "The advisory lane must use astral-sh/setup-uv."
        pinned = [str(s.get("with", {}).get("version", "")) for s in setup_uv_steps]
        assert any(v and v != "latest" for v in pinned), (
            "The advisory lane must pin a fixed uv version (setup-uv 'version:' "
            "input), not float on 'latest', so preview flag churn can't break it."
        )

    def test_blocking_pip_audit_gate_still_present(self) -> None:
        # The advisory lane runs ALONGSIDE the existing gate — it does not
        # replace it. The blocking gate keeps its name and pip-audit tool.
        jobs = _load_ci_jobs()
        assert _BLOCKING_JOB in jobs, "The blocking pip-audit gate must remain."
        joined = " ".join(_job_run_commands(jobs[_BLOCKING_JOB]))
        assert "pip-audit" in joined, "The blocking 'uv-audit' gate must still invoke pip-audit."

    def test_sbom_diff_gate_not_removed(self) -> None:
        # Ticket decision: run uv audit alongside the #2288 SBOM-diff gate,
        # do not remove the SBOM gate.
        jobs = _load_ci_jobs()
        assert "sbom" in jobs, "The #2288 SBOM-diff gate must not be removed."
        joined = " ".join(_job_run_commands(jobs["sbom"]))
        assert "git diff --exit-code dist/sbom.json" in joined, (
            "The SBOM-diff gate must keep failing on a stale dist/sbom.json."
        )


class TestUvAuditAllowlistConfig:
    def test_pyproject_has_uv_audit_allowlist_placeholder(self) -> None:
        # A documented, empty allowlist structure for accepted advisories.
        text = _PYPROJECT.read_text(encoding="utf-8")
        assert "[tool.teatree.uv_audit]" in text, (
            "pyproject.toml must declare a [tool.teatree.uv_audit] allowlist "
            "placeholder for accepted advisories (empty/documented)."
        )
        assert "ignore = [" in text.split("[tool.teatree.uv_audit]", 1)[1][:600], (
            "The [tool.teatree.uv_audit] section must document an 'ignore' allowlist key (empty placeholder is fine)."
        )
