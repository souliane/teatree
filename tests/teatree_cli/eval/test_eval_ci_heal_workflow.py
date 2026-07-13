"""Static contract checks for the ``eval-ci-heal`` workflow.

`eval-ci-heal.yml` is the workflow_dispatch-only workflow the CI-eval heal loop
dispatches. These pin its contract without running it: the trigger + inputs, both
credential secrets wired (the knob is a pure config flip), the no-silent-green
enforcement, that it reuses the exact in-Docker `t3 eval run --backend api
--docker --require-executed` invocation and emits the publish-safe `--summary-json`
artifact + the private transcript, and the injection-safe env-var pattern (no
caller input inlined into a run line).

The full suite (~231 scenarios) is FANNED OUT across a parallel matrix of shards
(souliane/teatree#3202): a `prepare` job computes the `{shard}` matrix, the `eval`
job runs one `--shard <index>/<total>` leg per matrix entry, and a `combine` job
merges every shard's per-scenario JSON into ONE `eval-heal-<sha>.json`. The eval
runs single-trial with `--escalate-on-fail` (the adaptive default) — the retired
blanket `--trials 2` pass@2 must be gone. These assertions FAIL if the sharding,
the combine job, or the adaptive single-trial invocation is missing.
"""

from pathlib import Path
from typing import Any, cast

import yaml

_WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "eval-ci-heal.yml"


def _load() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8")))


def _on(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML parses the unquoted ``on:`` key as the boolean True.
    return cast("dict[str, Any]", workflow.get("on", workflow.get(True)))


def _jobs() -> dict[str, Any]:
    return cast("dict[str, Any]", _load()["jobs"])


def _text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


class TestTrigger:
    def test_is_workflow_dispatch_only(self) -> None:
        on = _on(_load())
        assert set(on) == {"workflow_dispatch"}

    def test_exposes_the_inputs(self) -> None:
        inputs = _on(_load())["workflow_dispatch"]["inputs"]
        assert set(inputs) == {"scenarios", "shards", "credential", "pr_ref"}
        assert inputs["credential"]["default"] == "subscription_oauth"

    def test_shards_input_has_a_sensible_default(self) -> None:
        inputs = _on(_load())["workflow_dispatch"]["inputs"]
        # A default in the recommended 6-8 range so each shard's worst case stays
        # comfortably under the per-step timeout.
        assert 6 <= int(inputs["shards"]["default"]) <= 8


class TestCredentialWiring:
    def test_both_secrets_and_the_selector_are_wired(self) -> None:
        text = _text()
        assert "secrets.CLAUDE_CODE_OAUTH_TOKEN" in text
        assert "secrets.ANTHROPIC_API_KEY" in text
        assert "T3_EVAL_CREDENTIAL" in text


class TestNoSilentGreen:
    def test_asserts_claude_cli_and_require_executed(self) -> None:
        text = _text()
        assert "claude --version" in text
        assert "--require-executed" in text


class TestReusesTheDockerEvalInvocation:
    def test_runs_backend_api_in_docker(self) -> None:
        text = _text()
        assert "--backend api" in text
        assert "--docker" in text

    def test_emits_the_summary_json_artifact(self) -> None:
        assert "--summary-json" in _text()


class TestAdaptiveSingleTrial:
    def test_uses_escalate_on_fail_not_blanket_pass_at_2(self) -> None:
        text = _text()
        # The adaptive single-trial default: a passing scenario runs once, a real
        # failure escalates. The retired blanket pass@2 must be gone.
        assert "--escalate-on-fail" in text
        assert "--trials 2" not in text
        assert "--require any" not in text
        assert "EVAL_TRIALS" not in text


class TestSharding:
    def test_prepare_job_computes_the_shard_matrix(self) -> None:
        prepare = _jobs()["prepare"]
        assert "scripts/eval/shard_matrix.py" in _text()
        assert "matrix" in prepare["outputs"]
        assert "sha" in prepare["outputs"]

    def test_eval_job_fans_out_over_the_matrix(self) -> None:
        eval_job = _jobs()["eval"]
        assert eval_job["needs"] == "prepare"
        strategy = eval_job["strategy"]
        # A red shard must not cancel its siblings — each leg's verdict is independent.
        assert strategy["fail-fast"] is False
        assert "fromJSON(needs.prepare.outputs.matrix)" in str(strategy["matrix"]["include"])

    def test_eval_leg_runs_one_shard_of_the_catalog(self) -> None:
        assert "--shard" in _text()
        # The full-suite leg passes the matrix shard token through to the CLI.
        assert "matrix.shard" in _text()


class TestCombineJob:
    def test_combine_needs_prepare_and_eval(self) -> None:
        combine = _jobs()["combine"]
        assert set(combine["needs"]) == {"prepare", "eval"}
        # `always()` so a red shard still merges — a red run is when reds are triaged.
        assert "always()" in combine["if"]

    def test_combine_downloads_shard_jsons_and_merges_them(self) -> None:
        text = _text()
        assert "eval-heal-shard-${{ needs.prepare.outputs.sha }}-*" in text
        assert "merge-summary-json" in text

    def test_combine_uploads_the_one_eval_heal_sha_artifact(self) -> None:
        # The download path (`t3 eval ci-status`) resolves the artifact by the
        # `eval-heal-<sha>` name — the combine job is what produces it now.
        assert "eval-heal-${{ needs.prepare.outputs.sha }}" in _text()


class TestArtifacts:
    def test_uploads_the_per_shard_json_and_transcript(self) -> None:
        text = _text()
        assert "eval-heal-shard-${{ needs.prepare.outputs.sha }}-${{ steps.artifact.outputs.suffix }}" in text
        assert "eval-heal-transcript-${{ needs.prepare.outputs.sha }}-${{ steps.artifact.outputs.suffix }}" in text


class TestInjectionSafety:
    def test_caller_inputs_are_routed_through_env_not_inlined_into_run(self) -> None:
        # A caller-supplied input (scenarios / shards / credential / pr_ref) is bound
        # to an env var and referenced as $VAR in the shell — never interpolated as
        # ${{ inputs.* }} directly inside a `run:` / retry `command:` body.
        for line in _text().splitlines():
            stripped = line.strip()
            if stripped.startswith(("uv run", "echo ", "for ", "if ", "npm ", "claude ", "python ")):
                assert "${{ inputs." not in stripped, f"inlined input in a run line — {stripped!r}"
