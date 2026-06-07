"""Tests that the CI workflow wires the diff-scoped lane gate safely (#132).

The headline safety property is FAIL-SAFE-UNKNOWN, enforced at two
levels: the classifier (``tests/test_changed_lanes_classifier.py``) and
the workflow wiring (here). These tests pin the workflow so a future
edit cannot silently:

- gate a security/quality lane (semgrep, sbom, uv-audit) on the diff,
- gate a docs/markdown gate on the diff,
- drop the PR-only / push-and-PR trigger of any existing job,
- gate the heavy ``test`` lane such that a push-to-main could skip it.

The only sanctioned skip is the HEAVY ``test`` + ``mutation-diff``
lanes on a provably pure-docs PR diff.
"""

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

_PREFLIGHT = "preflight"
_HEAVY_LANES = ("test", "mutation-diff")
# Lanes that must NEVER be gated on the diff — always run.
_ALWAYS_RUN = (
    "sbom",
    "uv-audit",
    "docs-drift",
    "doc-update-gate",
    "blueprint-cross-pr",
    "comment-density-warning",
    "lint",
    "test-shape",
)


def _load_jobs() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"])


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return [needs]
    return list(needs)


class TestPreflightJob:
    def test_preflight_job_exists(self) -> None:
        assert _PREFLIGHT in _load_jobs(), "the diff-scoped lane classifier needs a 'preflight' job (#132)"

    def test_preflight_is_pr_only(self) -> None:
        # No base to diff on push/schedule, so preflight is PR-only and
        # the downstream if: conditions run heavy lanes on non-PR events.
        job = _load_jobs()[_PREFLIGHT]
        assert "pull_request" in str(job.get("if", ""))

    def test_preflight_invokes_the_classifier(self) -> None:
        steps = _load_jobs()[_PREFLIGHT]["steps"]
        joined = " ".join(str(step.get("run", "")) for step in steps if isinstance(step, dict))
        assert "scripts/ci/changed_lanes.py" in joined

    def test_preflight_exports_run_heavy_python(self) -> None:
        outputs = _load_jobs()[_PREFLIGHT].get("outputs", {})
        assert "run_heavy_python" in outputs

    def test_preflight_uses_full_fetch_depth(self) -> None:
        # The classifier needs the base..HEAD diff to resolve.
        steps = _load_jobs()[_PREFLIGHT]["steps"]
        checkout = next(
            s for s in steps if isinstance(s, dict) and str(s.get("uses", "")).startswith("actions/checkout")
        )
        assert checkout.get("with", {}).get("fetch-depth") == 0


class TestHeavyLanesGatedSafely:
    def test_heavy_lanes_depend_on_preflight(self) -> None:
        jobs = _load_jobs()
        for lane in _HEAVY_LANES:
            assert _PREFLIGHT in _needs(jobs[lane]), f"{lane} must need preflight to read its lane decision"

    def test_heavy_lanes_gate_on_run_heavy_python(self) -> None:
        jobs = _load_jobs()
        for lane in _HEAVY_LANES:
            condition = str(jobs[lane].get("if", ""))
            assert "run_heavy_python" in condition, f"{lane} must gate on preflight.outputs.run_heavy_python"

    def test_test_lane_runs_on_non_pr_events(self) -> None:
        # push-to-main and schedule have no diff to classify; the test
        # lane MUST still run (fail-safe). The condition allows it when
        # the event is not a pull_request.
        condition = str(_load_jobs()["test"].get("if", ""))
        assert "github.event_name != 'pull_request'" in condition

    def test_heavy_lane_condition_uses_always(self) -> None:
        # needs: preflight + a skipped preflight (push/schedule) would
        # otherwise skip the dependent job; always() lets it evaluate its
        # own condition so a push can never silently skip the test lane.
        for lane in _HEAVY_LANES:
            condition = str(_load_jobs()[lane].get("if", ""))
            assert "always()" in condition, f"{lane} must use always() so a skipped preflight cannot skip it on push"


class TestAlwaysRunLanesNotGated:
    def test_security_and_docs_lanes_do_not_depend_on_preflight(self) -> None:
        jobs = _load_jobs()
        for lane in _ALWAYS_RUN:
            assert _PREFLIGHT not in _needs(jobs[lane]), (
                f"{lane} is a security/quality or docs gate and must NEVER be gated on the diff (#132)"
            )

    def test_security_and_docs_lanes_do_not_gate_on_run_heavy_python(self) -> None:
        jobs = _load_jobs()
        for lane in _ALWAYS_RUN:
            condition = str(jobs[lane].get("if", ""))
            assert "run_heavy_python" not in condition, f"{lane} must not gate on the diff classification"


class TestExistingTriggersPreserved:
    def test_banned_terms_tree_stays_push_or_schedule(self) -> None:
        condition = str(_load_jobs()["banned-terms-tree"].get("if", ""))
        assert "push" in condition
        assert "schedule" in condition

    def test_pr_only_jobs_keep_pr_guard(self) -> None:
        jobs = _load_jobs()
        for lane in ("blueprint-cross-pr", "doc-update-gate", "comment-density-warning"):
            assert "pull_request" in str(jobs[lane].get("if", "")), f"{lane} must keep its pull_request guard"

    def test_lint_and_test_shape_have_no_event_gate(self) -> None:
        # lint and test-shape run on both push and PR (no if:). Gating
        # them on the diff would risk skipping a quality lane.
        jobs = _load_jobs()
        for lane in ("lint", "test-shape", "docs-drift", "sbom", "uv-audit"):
            assert "if" not in jobs[lane] or "run_heavy_python" not in str(jobs[lane]["if"])
