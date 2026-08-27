"""The CI eval workflows default to subscription OAuth and are RIGHT-SIZED (#2707 reversal).

These fitness tests parse the workflow YAML and pin the cost-urgent reversal: the
GitHub behavioral-eval workflow defaults to the subscription OAuth credential and
sizes the OAuth lane so a full fan-out cannot throttle the plan's usage window (a
SINGLE effort tier, a smaller trial count), while keeping the metered key
selectable via the knob. The GitLab lane stays explicitly metered — it bills a real
key — while carrying NO `--gate-cost-bounds`, because its `anthropic_api` backend
reports no per-scenario cost for that gate to check. They go RED if the default
regresses to metered-exclusive, if the OAuth lane's fan-out is un-sized, or if the
unsatisfiable cost gate is re-attached to an unmetered lane.
"""
# test-path: cross-cutting — a CI-workflow fitness test that parses the pipeline YAML and
# imports the eval backend vocabulary, because "which lane carries which gate" is one
# decision spanning both; it mirrors neither src/teatree/cli/ nor src/teatree/eval/ alone.

from pathlib import Path
from typing import Any, cast

import yaml

from teatree.eval.backends import ANTHROPIC_API_BACKEND
from tests._ci_config import gitlab_ci_path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GH_EVAL = _REPO_ROOT / ".github" / "workflows" / "eval.yml"
_GH_EVAL_PR = _REPO_ROOT / ".github" / "workflows" / "eval-pr.yml"
_GITLAB_CI = gitlab_ci_path()


def _load(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def _gh_inputs() -> dict[str, Any]:
    workflow = _load(_GH_EVAL)
    on = cast("dict[str, Any]", workflow.get("on", workflow.get(True)))
    return cast("dict[str, Any]", on["workflow_dispatch"]["inputs"])


def _gh_eval_step_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for step in cast("list[dict[str, Any]]", _load(_GH_EVAL)["jobs"]["eval"]["steps"]):
        env.update(cast("dict[str, str]", step.get("env", {})))
    return env


def _gh_eval_run_command() -> str:
    for step in cast("list[dict[str, Any]]", _load(_GH_EVAL)["jobs"]["eval"]["steps"]):
        command = step.get("with", {}).get("command", "")
        if "t3 eval run" in command:
            return cast("str", command)
    msg = "the eval workflow has no `t3 eval run` step."
    raise AssertionError(msg)


class TestGitHubEvalDefaultsToSubscriptionOAuth:
    def test_credential_input_defaults_to_subscription_oauth(self) -> None:
        credential = _gh_inputs().get("credential")
        assert credential is not None, "the workflow must expose a `credential` input (the agent_harness_provider pin)."
        assert credential["default"] == "subscription_oauth", (
            "the eval lane must DEFAULT to subscription_oauth (reverses #2707)."
        )

    def test_eval_step_wires_both_secrets_and_the_knob(self) -> None:
        # The "Select the freshest eval OAuth account" step owns the credential decision:
        # it wires both secrets + the EVAL_OAUTH_TOKENS pool and resolves the `credential`
        # knob (EVAL_CREDENTIAL), exporting the chosen CLAUDE_CODE_OAUTH_TOKEN +
        # T3_AGENT_HARNESS_PROVIDER into $GITHUB_ENV. The aggregated job env still carries
        # both secrets and the knob.
        env = _gh_eval_step_env()
        assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}"
        assert env.get("EVAL_OAUTH_TOKENS") == "${{ secrets.EVAL_OAUTH_TOKENS }}"
        assert env.get("ANTHROPIC_API_KEY") == "${{ secrets.ANTHROPIC_API_KEY }}"
        assert env.get("EVAL_CREDENTIAL") == "${{ inputs.credential || 'subscription_oauth' }}"


class TestGitHubOAuthLaneIsRightSized:
    def test_efforts_input_defaults_to_a_single_tier(self) -> None:
        default = _gh_inputs()["efforts"]["default"]
        assert "," not in default, (
            f"the OAuth lane must default to a SINGLE effort tier (not low,medium,high); got {default!r}."
        )
        assert default == "high", "the single representative tier is `high`."

    def test_matrix_effort_fallback_is_a_single_tier(self) -> None:
        # The scheduled weekly cron runs the BASELINE (an EMPTY effort axis +
        # `--preset baseline`, one leg per scenario — see
        # test_eval_ci_require_executed_gate.py), never a 3-tier fan. A manual run
        # with a blank `efforts` field falls back to the single 'high' tier —
        # computed in shell (`EVAL_EFFORTS="${INPUT_EFFORTS:-high}"`) because a
        # GitHub-Actions `x && '' || y` ternary can never yield the empty string the
        # scheduled leg needs. Never the money-burning unconditional low,medium,high
        # axis (souliane/teatree#2878).
        text = _GH_EVAL.read_text(encoding="utf-8")
        assert "inputs.efforts || 'low,medium,high'" not in text, (
            "the matrix effort fallback must not restore the UNCONDITIONAL 3x low,medium,high axis."
        )
        assert "'schedule' && 'low,medium,high'" not in text, (
            "the weekly cron must be the cheap baseline — never a schedule-keyed 3-tier fan."
        )
        assert "${INPUT_EFFORTS:-high}" in text, (
            "a blank manual `efforts` field must fall back to the single 'high' tier."
        )

    def test_trials_input_defaults_below_three(self) -> None:
        default = int(_gh_inputs()["trials"]["default"])
        assert default < 3, f"the OAuth lane must default to fewer trials than the metered 3; got {default}."

    def test_eval_command_threads_the_trials_input(self) -> None:
        # The hard-coded `--trials 3` is replaced by the right-sizing EVAL_TRIALS var.
        command = _gh_eval_run_command()
        assert '--trials "$EVAL_TRIALS"' in command
        assert "--trials 3" not in command


class TestGitHubPrEvalRidesSubscriptionOAuth:
    def test_pr_eval_step_defaults_to_subscription_oauth(self) -> None:
        # The credential knob now rides the select step's EVAL_CREDENTIAL; the eval step
        # reads the resolved T3_AGENT_HARNESS_PROVIDER + CLAUDE_CODE_OAUTH_TOKEN from
        # $GITHUB_ENV.
        env: dict[str, str] = {}
        for step in cast("list[dict[str, Any]]", _load(_GH_EVAL_PR)["jobs"]["eval"]["steps"]):
            env.update(cast("dict[str, str]", step.get("env", {})))
        assert env.get("EVAL_CREDENTIAL") == "subscription_oauth"
        assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}"


class TestGitLabEvalLaneStaysMeteredWithoutTheUnsatisfiableCostGate:
    """The lane bills a real key, and asks it for no cost verdict the backend cannot give."""

    def _eval_suite_script(self) -> str:
        return "\n".join(cast("list[str]", _load(_GITLAB_CI)[".eval-suite"]["script"]))

    def test_gitlab_explicitly_selects_the_metered_key(self) -> None:
        # An unset knob resolves to subscription OAuth, which no runner holds — the miss
        # surfaces as an opaque auth error mid-suite rather than at the credential gate.
        assert _load(_GITLAB_CI)["variables"]["T3_AGENT_HARNESS_PROVIDER"] == "api_key"

    def test_the_lane_carries_no_cost_gate_its_backend_cannot_satisfy(self) -> None:
        # `anthropic_api` drives PydanticAiRunner, which reports no cost_usd, so every
        # ceiling reads MISSING and an empty set reads VACUOUS — red either way, and
        # calibrating a bound makes it worse. `t3 eval run` now exits 2 on the pairing.
        script = self._eval_suite_script()
        assert f"--backend {ANTHROPIC_API_BACKEND}" in script
        assert "--gate-cost-bounds" not in script, (
            f"--gate-cost-bounds is unsatisfiable on --backend {ANTHROPIC_API_BACKEND}: it reports no "
            "cost, so the gate can only ever go MISSING/VACUOUS red. Move it to a cost-recording "
            "backend, never back onto this lane."
        )

    def test_no_lane_smuggles_the_cost_gate_back_in_through_its_args(self) -> None:
        # Every eval job extends `.eval-suite` and inherits its `anthropic_api` backend,
        # so a per-lane LANE_ARGS carrying the flag is the same unsatisfiable pairing.
        config = _load(_GITLAB_CI)
        for name, job in config.items():
            variables = job.get("variables", {}) if isinstance(job, dict) else {}
            assert "--gate-cost-bounds" not in str(variables.get("LANE_ARGS", "")), (
                f"{name} smuggles the unsatisfiable cost gate in through LANE_ARGS."
            )
