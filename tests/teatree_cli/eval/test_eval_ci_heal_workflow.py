"""Static contract checks for the ``eval-ci-heal`` workflow.

`eval-ci-heal.yml` is the workflow_dispatch-only workflow the CI-eval heal loop
dispatches. These pin its contract without running it: the trigger + inputs, both
credential secrets wired (the knob is a pure config flip), the no-silent-green
enforcement, that it reuses the exact in-Docker `t3 eval run --backend api
--docker --require-executed` invocation and emits the publish-safe `--summary-json`
artifact + the private transcript, and the injection-safe env-var pattern (no
caller input inlined into a run line).
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


def _text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


class TestTrigger:
    def test_is_workflow_dispatch_only(self) -> None:
        on = _on(_load())
        assert set(on) == {"workflow_dispatch"}

    def test_exposes_the_three_inputs(self) -> None:
        inputs = _on(_load())["workflow_dispatch"]["inputs"]
        assert set(inputs) == {"scenarios", "credential", "pr_ref"}
        assert inputs["credential"]["default"] == "subscription_oauth"


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


class TestArtifacts:
    def test_uploads_the_publish_safe_json_and_the_private_transcript(self) -> None:
        text = _text()
        assert "eval-heal-${{ steps.sha.outputs.sha }}" in text
        assert "eval-heal-transcript-${{ steps.sha.outputs.sha }}" in text


class TestInjectionSafety:
    def test_caller_inputs_are_routed_through_env_not_inlined_into_run(self) -> None:
        # A caller-supplied input (scenarios / credential / pr_ref) is bound to an
        # env var and referenced as $VAR in the shell — never interpolated as
        # ${{ inputs.* }} directly inside a `run:` / retry `command:` body.
        for line in _text().splitlines():
            stripped = line.strip()
            if stripped.startswith(("uv run", "echo ", "for ", "if ", "npm ", "claude ")):
                assert "${{ inputs." not in stripped, f"inlined input in a run line — {stripped!r}"
