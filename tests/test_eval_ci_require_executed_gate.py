"""The metered eval workflow fails loud instead of passing an all-skipped green.

The eval runner SKIPs every scenario when ``claude`` is not on PATH / not
authenticated, and a fully-skipped suite reports green with zero behavioral
coverage. The fix relocates the metered ``claude -p`` suite OUT of the PR
pipeline into a standalone weekly/manual workflow that passes
``--require-executed`` UNCONDITIONALLY (never key-gated — the original bug armed
the guard only when a credential was set, i.e. gated on the exact condition it
exists to catch) and installs + asserts the Claude CLI so a missing binary FAILS
the job.

Auth is the ``eval_credential`` knob's call — #2707 is REVERSED, so the eval lane
DEFAULTS to the subscription ``CLAUDE_CODE_OAUTH_TOKEN`` (both secrets wired so the
metered ``ANTHROPIC_API_KEY`` stays selectable via the knob).

These are the recurrence-proof fitness tests: they parse the workflow YAML and
assert the eval invocation always carries ``--require-executed`` and is NOT
key-conditional, that the default wires the subscription OAuth token (with the
metered key still selectable), and that ``ci.yml`` no longer carries an eval job on
the PR path. They go RED if ``--require-executed`` is removed.
"""

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GH_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_GH_EVAL = _REPO_ROOT / ".github" / "workflows" / "eval.yml"
_GITLAB_CI = _REPO_ROOT / ".gitlab-ci.yml"

_FLAG = "--require-executed"


def _gh_eval_run_command() -> str:
    jobs = cast("dict[str, Any]", yaml.safe_load(_GH_EVAL.read_text(encoding="utf-8"))["jobs"])
    steps = cast("list[dict[str, Any]]", jobs["eval"]["steps"])
    for step in steps:
        command = step.get("with", {}).get("command", "")
        if "t3 eval run" in command:
            return command
    msg = "the eval workflow has no step running `t3 eval run`."
    raise AssertionError(msg)


def _gh_eval_workflow_text() -> str:
    return _GH_EVAL.read_text(encoding="utf-8")


def _gh_eval_step_env() -> dict[str, str]:
    jobs = cast("dict[str, Any]", yaml.safe_load(_GH_EVAL.read_text(encoding="utf-8"))["jobs"])
    env: dict[str, str] = {}
    for step in cast("list[dict[str, Any]]", jobs["eval"]["steps"]):
        env.update(cast("dict[str, str]", step.get("env", {})))
    return env


def _gitlab_eval_script() -> list[str]:
    config = cast("dict[str, Any]", yaml.safe_load(_GITLAB_CI.read_text(encoding="utf-8")))
    # The script is the shared `.eval-suite` body extended by the eval jobs.
    return cast("list[str]", config[".eval-suite"]["script"])


class TestGitHubRequireExecutedUnconditional:
    def test_eval_run_command_carries_the_flag_inline(self) -> None:
        command = _gh_eval_run_command()
        assert _FLAG in command, (
            "The metered eval `t3 eval run` step must carry --require-executed inline so a "
            "decorative all-skipped run can't pass green."
        )

    def test_flag_is_not_key_conditional(self) -> None:
        # The original bug armed the guard ONLY when a key was set — gated on the
        # exact condition it exists to catch. The flag must be passed literally,
        # never interpolated from a key-conditional output.
        command = _gh_eval_run_command()
        assert "require_executed" not in command.replace(_FLAG, ""), (
            "--require-executed must be passed unconditionally, not via a key-gated "
            "${{ steps.*.outputs.require_executed }} interpolation."
        )
        # The flag must not sit behind any credential conditional anywhere in the
        # eval workflow (no `if [ -n "$ANTHROPIC_API_KEY" ]` arming step).
        text = _gh_eval_workflow_text()
        assert 'if [ -n "$ANTHROPIC_API_KEY" ]' not in text, (
            "The eval workflow must not gate --require-executed on the API key."
        )
        assert 'if [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ]' not in text, (
            "The eval workflow must not gate --require-executed on a credential."
        )

    def test_workflow_installs_and_asserts_the_claude_cli(self) -> None:
        text = _gh_eval_workflow_text()
        assert "claude --version" in text, (
            "The eval workflow must assert the Claude CLI install (`claude --version`) so a "
            "missing binary fails the job instead of skipping every scenario."
        )

    def test_metered_lane_runs_through_the_container(self) -> None:
        # "All metered eval in Docker" must hold in CI too: the runner has Docker,
        # so the metered `t3 eval run` step routes through dev/Dockerfile.test (the
        # `--docker` force, or — equivalently — the default-Docker path with no
        # --local). The container ships the Claude CLI, so the run is reproducible.
        command = _gh_eval_run_command()
        assert "--docker" in command, "The CI metered eval must run IN the container (--docker)."
        assert "--local" not in command, "The CI metered eval must never use --local (a host run)."

    def test_default_wires_the_oauth_token_and_keeps_the_metered_key_selectable(self) -> None:
        # #2707 is REVERSED: the eval lane DEFAULTS to the subscription OAuth token.
        # Both secrets are wired so the `credential` knob (T3_EVAL_CREDENTIAL) is a
        # pure config flip; the default resolves subscription_oauth.
        env = _gh_eval_step_env()
        assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}", (
            "The eval step must wire CLAUDE_CODE_OAUTH_TOKEN from the repo secret — the eval "
            "lane defaults to the subscription OAuth token (#2707 reversal)."
        )
        assert env.get("ANTHROPIC_API_KEY") == "${{ secrets.ANTHROPIC_API_KEY }}", (
            "The metered ANTHROPIC_API_KEY must stay wired so `metered_api_key` is selectable "
            "via the credential knob without editing the workflow."
        )
        assert env.get("T3_EVAL_CREDENTIAL") == "${{ inputs.credential || 'subscription_oauth' }}", (
            "The eval step must resolve the credential knob (default subscription_oauth)."
        )


class TestGitHubScheduledGuardManualUnguarded:
    """The scheduled path is no-PR-guarded; the manual dispatch always runs.

    The gate decision lives in the ``prepare`` job (which also computes the lane
    matrix the ``eval`` job fans out over, #2492); the ``eval`` job is gated on
    that decision at the job level (``if: needs.prepare.outputs.run_eval``).
    """

    def _gate_step_run(self) -> str:
        jobs = cast("dict[str, Any]", yaml.safe_load(_GH_EVAL.read_text(encoding="utf-8"))["jobs"])
        for step in cast("list[dict[str, Any]]", jobs["prepare"]["steps"]):
            if step.get("id") == "gate":
                return cast("str", step["run"])
        msg = "the prepare job has no `gate` step deciding whether to run."
        raise AssertionError(msg)

    def test_manual_dispatch_forces_a_run(self) -> None:
        run = self._gate_step_run()
        assert "workflow_dispatch" in run, "The gate must branch on the workflow_dispatch event."
        assert "run_eval=true" in run, "The manual workflow_dispatch branch must force run_eval=true."

    def test_scheduled_path_runs_the_no_pr_guard(self) -> None:
        run = self._gate_step_run()
        assert "merged_prs_since.py" in run, (
            "The scheduled path must run the no-PR pre-check (merged_prs_since.py) so a cron "
            "tick with nothing new merged skips cleanly."
        )

    def test_eval_job_is_gated_on_the_decision_not_the_invocation(self) -> None:
        # The PRE-CHECK gates whether the eval JOB runs (job-level `if` on the
        # prepare decision); it must NOT weaken the eval invocation itself (which
        # always carries --require-executed).
        jobs = cast("dict[str, Any]", yaml.safe_load(_GH_EVAL.read_text(encoding="utf-8"))["jobs"])
        eval_job = cast("dict[str, Any]", jobs["eval"])
        assert eval_job.get("if", "") == "needs.prepare.outputs.run_eval == 'true'", (
            "The metered eval job must be gated on the prepare gate decision at the job level."
        )
        assert "prepare" in eval_job.get("needs", ""), "The eval job must depend on the prepare gate job."
        assert _FLAG in _gh_eval_run_command(), (
            "The gated eval invocation must still carry --require-executed (the guard "
            "decides whether to invoke, not whether the eval may silently skip-as-pass)."
        )

    def test_eval_fans_out_one_lane_shard_per_matrix_leg(self) -> None:
        # #2492/#2683: the full ~196-scenario suite does not fit the 2x80min budget,
        # and BOTH a single ~182-scenario clean_room leg AND a single under_load 1/1
        # leg hit the same wall, so each matrix leg meters ONE {lane, shard} (from
        # the prepare job's validated plan, lane-aware ceiling).
        jobs = cast("dict[str, Any]", yaml.safe_load(_GH_EVAL.read_text(encoding="utf-8"))["jobs"])
        matrix = cast("dict[str, Any]", jobs["eval"]["strategy"]["matrix"])
        assert "include" in matrix, "The eval job must fan out over a {lane, shard} include matrix."
        assert "fromJSON(needs.prepare.outputs.matrix)" in matrix["include"], (
            "The matrix must come from the prepare job's computed {lane, shard} plan."
        )
        command = _gh_eval_run_command()
        assert '--lane "$EVAL_LANE"' in command, "Each leg must scope `t3 eval run` to its one matrix lane."
        assert '--shard "$EVAL_SHARD"' in command, "Each leg must scope `t3 eval run` to its one matrix shard."
        prepare_steps = cast("list[dict[str, Any]]", jobs["prepare"]["steps"])
        assert any("lane_matrix.py" in step.get("run", "") for step in prepare_steps), (
            "The prepare job must compute the {lane, shard} matrix via lane_matrix.py."
        )

    def test_each_emitted_leg_meters_a_budget_safe_scenario_count(self) -> None:
        # The blocking-finding fix: the matrix lane_matrix.py emits must bound EVERY
        # leg's scenario count to its lane's budget-safe ceiling — not just
        # clean_room. The ceiling is LANE-AWARE (#2683): under_load's roster-
        # spawning scenarios (10-45 min each) get a much smaller ceiling than
        # clean_room's. Computed against the LIVE catalog the workflow runs, so a
        # catalog that grows past a lane's bound without re-sharding turns this RED.
        from teatree.eval.discovery import discover_specs  # noqa: PLC0415
        from teatree.eval.lane_shard import (  # noqa: PLC0415
            filter_specs_by_shard,
            max_scenarios_per_shard,
            plan_lane_shards,
        )
        from teatree.eval.models import PERMITTED_LANES, UNDER_LOAD_LANE  # noqa: PLC0415

        specs = discover_specs()
        legs = plan_lane_shards(specs, sorted(PERMITTED_LANES))
        # BOTH lanes must split: clean_room (~182, the cold-review finding) AND
        # under_load (roster-spawning, the 80min-cap finding #2683). Each permitted
        # lane therefore contributes more than one leg, so the total exceeds 2x the
        # lane count.
        assert len(legs) > 2 * len(PERMITTED_LANES), (
            "every permitted lane must split into multiple shards: clean_room (~182 scenarios) "
            "and under_load (roster-spawning, 10-45 min/scenario, #2683), not one leg each."
        )
        assert sum(1 for leg in legs if leg.lane == UNDER_LOAD_LANE) > 1, (
            "under_load must be sharded into multiple legs (#2683), not run as a single 1/1 leg "
            "that hits the 80min step cap."
        )
        for leg in legs:
            lane_specs = [s for s in specs if s.lane == leg.lane]
            shard_specs = filter_specs_by_shard(lane_specs, leg.shard)
            bound = max_scenarios_per_shard(leg.lane)
            assert len(shard_specs) <= bound, (
                f"emitted leg {leg.lane} {leg.shard} meters {len(shard_specs)} scenarios, over the "
                f"lane's budget-safe bound {bound}."
            )


class TestGitHubCiHasNoMeteredEvalOnPrPath:
    def test_ci_yml_has_no_eval_job(self) -> None:
        jobs = cast("dict[str, Any]", yaml.safe_load(_GH_CI.read_text(encoding="utf-8"))["jobs"])
        eval_jobs = [name for name in jobs if "eval" in name.lower()]
        assert eval_jobs == [], (
            f"ci.yml must not define a metered eval job on the PR path; found {eval_jobs}. "
            "The metered suite lives in .github/workflows/eval.yml."
        )

    def test_ci_yml_does_not_invoke_the_metered_suite(self) -> None:
        # Inspect executable step bodies (run: + retry-action command:), not raw
        # text — a comment pointing readers to eval.yml is fine; an actual
        # invocation is the regression.
        jobs = cast("dict[str, Any]", yaml.safe_load(_GH_CI.read_text(encoding="utf-8"))["jobs"])
        for job_name, job in jobs.items():
            for step in cast("list[dict[str, Any]]", job.get("steps", [])):
                body = f"{step.get('run', '')}\n{step.get('with', {}).get('command', '')}"
                assert "t3 eval run" not in body, (
                    f"ci.yml job {job_name!r} must not invoke `t3 eval run` (the metered suite) — "
                    "it relocated to eval.yml."
                )


class TestGitLabRequireExecutedUnconditional:
    def test_eval_run_line_carries_the_flag(self) -> None:
        joined = "\n".join(_gitlab_eval_script())
        assert _FLAG in joined, "The GitLab eval script must carry --require-executed on `t3 eval run`."

    def test_flag_is_not_key_conditional(self) -> None:
        # No `if [ -n "$ANTHROPIC_API_KEY" ]; then REQUIRE_EXECUTED=...` arming.
        joined = "\n".join(_gitlab_eval_script())
        assert 'if [ -n "$ANTHROPIC_API_KEY" ]' not in joined, (
            "The GitLab gate must not arm --require-executed conditionally on the key."
        )
        assert "$REQUIRE_EXECUTED" not in joined, (
            "--require-executed must be passed literally, not via a key-conditional shell var."
        )

    def test_gitlab_installs_and_asserts_the_claude_cli(self) -> None:
        config = cast("dict[str, Any]", yaml.safe_load(_GITLAB_CI.read_text(encoding="utf-8")))
        before = "\n".join(cast("list[str]", config[".eval-suite"]["before_script"]))
        assert "claude --version" in before, (
            "The GitLab eval-suite must assert the Claude CLI install so a missing binary fails."
        )

    def test_metered_eval_is_not_on_merge_request_pipelines(self) -> None:
        config = cast("dict[str, Any]", yaml.safe_load(_GITLAB_CI.read_text(encoding="utf-8")))
        for job in ("eval-weekly", "eval-manual"):
            rules = cast("list[dict[str, Any]]", config[job]["rules"])
            for rule in rules:
                condition = rule.get("if", "")
                on_mr = "merge_request_event" in condition and not condition.strip().startswith(
                    "$CI_PIPELINE_SOURCE !="
                )
                assert not on_mr, f"{job} must not run on merge-request pipelines; rule condition was {condition!r}."

    def test_scheduled_path_is_no_pr_guarded(self) -> None:
        config = cast("dict[str, Any]", yaml.safe_load(_GITLAB_CI.read_text(encoding="utf-8")))
        # The scheduled eval is gated on a RUN_EVAL flag the eval-gate job sets.
        weekly_rules = cast("list[dict[str, Any]]", config["eval-weekly"]["rules"])
        assert any("RUN_EVAL" in rule.get("if", "") for rule in weekly_rules), (
            "eval-weekly (the scheduled path) must be gated on the eval-gate RUN_EVAL flag."
        )
        gate_script = "\n".join(cast("list[str]", config["eval-gate"]["script"]))
        assert "merged_prs_since.py" in gate_script, "eval-gate must run the no-PR pre-check (merged_prs_since.py)."

    def test_manual_path_is_unguarded(self) -> None:
        # eval-manual must NOT depend on the gate flag — a maintainer force-runs.
        config = cast("dict[str, Any]", yaml.safe_load(_GITLAB_CI.read_text(encoding="utf-8")))
        manual_rules = cast("list[dict[str, Any]]", config["eval-manual"]["rules"])
        assert not any("RUN_EVAL" in rule.get("if", "") for rule in manual_rules), (
            "eval-manual must be unguarded (the manual run always runs, no-PR guard bypassed)."
        )
