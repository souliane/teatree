"""Static contract checks for the reusable (workflow_call) eval workflows.

The selective-PR and weekly metered eval are the SAME logic for the teatree host
and every overlay, so they live in `eval-pr-reusable.yml` / `eval-weekly-reusable.yml`
as `workflow_call` workflows an overlay's thin caller `uses:`-references. These
pin the reusable contract: the `workflow_call` trigger + required secret, the
optional overlay host-checkout/scenario-assertion inputs, the no-silent-green
enforcement (`--require-executed` + asserted `claude --version`), and that the
mechanics route through the reusable `t3 eval` CLI primitives — not a duplicated
inline script. They also guard the env-var-safe injection pattern (no
`assert-scenarios` / `lane` value inlined into a `run:` shell).
"""

from pathlib import Path
from typing import Any, cast

import yaml

_WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
_PR = _WORKFLOWS / "eval-pr-reusable.yml"
_WEEKLY = _WORKFLOWS / "eval-weekly-reusable.yml"
_BENCHMARK = _WORKFLOWS / "eval-benchmark.yml"


def _load(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def _on(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML parses the unquoted ``on:`` key as the boolean True.
    return cast("dict[str, Any]", workflow.get("on", workflow.get(True)))


class TestReusableContract:
    def test_both_are_workflow_call_with_credential_secrets(self) -> None:
        # eval-pr-reusable requires the metered api-key; eval-weekly-reusable is
        # dual-credential (subscription_oauth default), so it declares both secrets
        # and the runner enforces the selected one.
        pr_secrets = _on(_load(_PR))["workflow_call"]["secrets"]
        assert pr_secrets["anthropic-api-key"]["required"] is True, _PR.name
        weekly_secrets = _on(_load(_WEEKLY))["workflow_call"]["secrets"]
        assert "subscription-oauth-token" in weekly_secrets, _WEEKLY.name
        assert "anthropic-api-key" in weekly_secrets, _WEEKLY.name

    def test_overlay_host_checkout_inputs_are_exposed(self) -> None:
        for path in (_PR, _WEEKLY):
            inputs = _on(_load(path))["workflow_call"]["inputs"]
            assert "teatree-repo" in inputs, path.name
            assert "assert-scenarios" in inputs, path.name

    def test_weekly_exposes_force_and_dashboard_path(self) -> None:
        inputs = _on(_load(_WEEKLY))["workflow_call"]["inputs"]
        assert "force" in inputs
        assert "dashboard-path" in inputs

    def test_weekly_exposes_shards_defaulting_to_unfiltered(self) -> None:
        inputs = _on(_load(_WEEKLY))["workflow_call"]["inputs"]
        assert inputs["shards"]["default"] == ""

    def test_benchmark_caller_exposes_shards_and_threads_it_to_the_reusable(self) -> None:
        dispatch_inputs = _on(_load(_BENCHMARK))["workflow_dispatch"]["inputs"]
        assert dispatch_inputs["shards"]["default"] == ""
        benchmark_job = _load(_BENCHMARK)["jobs"]["benchmark"]
        assert benchmark_job["with"]["shards"] == "${{ inputs.shards }}"


class TestNoSilentGreen:
    def test_both_assert_claude_cli_and_require_executed(self) -> None:
        for path in (_PR, _WEEKLY):
            text = path.read_text(encoding="utf-8")
            assert "claude --version" in text, path.name
            assert "--require-executed" in text, path.name


class TestReusesCliPrimitives:
    def test_pr_eval_selects_via_changed_scenarios_cli(self) -> None:
        assert "t3 eval changed-scenarios" in _PR.read_text(encoding="utf-8")

    def test_weekly_guards_via_merged_prs_since_cli(self) -> None:
        assert "t3 eval merged-prs-since" in _WEEKLY.read_text(encoding="utf-8")

    def test_weekly_runs_the_three_tier_benchmark(self) -> None:
        # The weekly metered run is the canonical benchmark: every scenario against
        # all three tier models via `t3 eval run --benchmark`, publishing the
        # self-contained matrix HTML dashboard.
        text = _WEEKLY.read_text(encoding="utf-8")
        assert "t3 eval run --benchmark" in text
        assert "eval-benchmark-" in text


class TestPublishGuard:
    """The publish job refuses shards not backed by metered spend, before it commits."""

    def _publish_step_names(self) -> list[str]:
        steps = _load(_WEEKLY)["jobs"]["publish"]["steps"]
        return [step.get("name", "") for step in steps]

    def test_guard_runs_before_the_dashboard_is_written_or_committed(self) -> None:
        names = self._publish_step_names()
        guard = next(i for i, name in enumerate(names) if "not backed by metered spend" in name)
        collect = next(i for i, name in enumerate(names) if "Collect the shard matrices" in name)
        commit = next(i for i, name in enumerate(names) if name.startswith("Commit and push"))
        assert guard < collect < commit

    def test_guard_invokes_the_cli_primitive(self) -> None:
        assert "t3 eval verify-benchmark-publish" in _WEEKLY.read_text(encoding="utf-8")


class TestInjectionSafety:
    def test_caller_values_are_routed_through_env_not_inlined_into_run(self) -> None:
        # The env-var-safe pattern: a caller-supplied value (assert-scenarios,
        # lane) is bound to an env var and referenced as $VAR in the shell, never
        # interpolated as ${{ inputs.* }} directly inside a `run:` body.
        for path in (_PR, _WEEKLY, _BENCHMARK):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith(("uv run", "echo ", "grep ", "git ")):
                    assert "${{ inputs." not in stripped, f"{path.name}: inlined input in a run line — {stripped!r}"

    def test_shards_is_routed_through_env_not_inlined_into_run(self) -> None:
        text = _WEEKLY.read_text(encoding="utf-8")
        assert "EVAL_SHARDS: ${{ inputs.shards }}" in text
        assert '--shards "$EVAL_SHARDS"' in text
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("uv run", "echo ", "grep ", "git ")):
                assert "${{ inputs.shards" not in stripped, f"inlined shards input in a run line — {stripped!r}"
