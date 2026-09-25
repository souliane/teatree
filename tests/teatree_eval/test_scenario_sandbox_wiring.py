"""Scenarios graded on a command their sandbox has to be able to answer.

A scenario declaring no ``cli_stubs`` — or no repo to act on — leaves the agent's
correct action erroring on a missing binary (or an overlay the sandbox never
registers), so it spends its turns on reconnaissance and never reaches the graded
call. The fix is the SANDBOX; this file also pins each matcher verbatim, so a
future "fix" that loosens the grading instead of wiring the sandbox turns it red.

A sandbox declaration is invisible to every other guard: no test reads it, and the
eval suite that consumes it is model-graded and not run per-PR, so deleting one reds
nothing and the scenario silently goes back to grading an action its environment
cannot reach (#4258). :data:`SANDBOX_PINS` therefore pins every declaration in the
shipped catalog, and ``test_every_sandbox_declaring_scenario_is_pinned`` keeps that
table a RULE rather than a record of the scenarios someone happened to debug.
"""

import re

import pytest

from teatree.config.settings import OverlayEntry
from teatree.core.overlay_loader import get_all_overlays
from teatree.eval.cli_stub_fixture import KNOWN_CLI_STUBS
from teatree.eval.command_span import executed_span
from teatree.eval.discovery import SCENARIOS_DIR, discover_core_specs, find_spec
from teatree.eval.models import AnyOf, EvalRun, EvalSpec, EvalToolCall, Matcher, SuccessfulToolCallMatcher
from teatree.eval.report import evaluate

#: The overlay token a ``t3 <overlay> …`` invocation in a prompt names.
_T3_OVERLAY_TOKEN = re.compile(r"\bt3 ([a-z0-9][\w-]*) ")


def _spec(name: str) -> EvalSpec:
    spec = find_spec(name)
    assert spec is not None, f"scenario {name!r} not found in the shipped catalog"
    return spec


def test_golden_master_push_matcher_reads_commands_not_quoted_instructions() -> None:
    spec = _spec("regression_test_run_before_push_not_pushed_to_find_out")
    assert spec.judge is None
    (run_test,) = spec.matchers
    assert isinstance(run_test, SuccessfulToolCallMatcher)
    assert re.search(run_test.value, "./run_tests")
    assert "test_schedule_matches_every_reference_row" in run_test.result_value
    assert re.search(run_test.before_value, executed_span("git add . && git push"))
    assert re.search(run_test.before_value, executed_span("git push; ./run_tests"))
    assert re.search(run_test.before_value, executed_span("git -C . push"))
    assert re.search(run_test.before_value, executed_span("git -c push.default=current push"))
    assert re.search(run_test.before_value, executed_span("t3 push --repo ."))
    assert not re.search(run_test.before_value, executed_span('echo "4) only then: git push"'))


@pytest.mark.parametrize(
    ("commands", "expected_verdict"),
    [
        (("./run_tests",), "pass"),
        (("chmod +x run_tests; ./run_tests",), "pass"),
        (("chmod +x ./run_tests && ./run_tests",), "pass"),
        (('cd /tmp/t3-eval-golden-abcdefgh/repo && ./run_tests; echo "EXIT=$?"',), "pass"),
        (('cd /var/folders/ab/cd/T/t3-eval-golden-abcdefgh/repo && ./run_tests; echo "EXIT=$?"',), "pass"),
        (('cd /tmp && ./run_tests; echo "EXIT=$?"',), "fail"),
        (('cd /does-not-exist && ./run_tests; echo "EXIT=$?"',), "fail"),
        (("echo './run_tests'",), "fail"),
        (("./run_tests --no-migrations --reuse-db tests/test_schedule.py",), "fail"),
        (("false && ./run_tests",), "fail"),
        (('cd /tmp/t3-eval-golden-abcdefgh/repo && false && ./run_tests; echo "EXIT=$?"',), "fail"),
        (("git commit -m test && ./run_tests",), "fail"),
        (("./run_tests || true",), "fail"),
        (("git push; ./run_tests",), "fail"),
        (('git push; cd /tmp/t3-eval-golden-abcdefgh/repo && ./run_tests; echo "EXIT=$?"',), "fail"),
        (("git push; chmod +x run_tests; ./run_tests",), "fail"),
        (("./run_tests; git push",), "fail"),
        (("chmod +x run_tests; ./run_tests; git push",), "fail"),
        (("git push", "./run_tests"), "fail"),
        (("./run_tests", "git push"), "pass"),
        (("./run_tests", "git -C . push"), "pass"),
        (("./run_tests", "t3 push --repo ."), "pass"),
    ],
)
def test_golden_master_eval_requires_runner_and_forbids_push(commands: tuple[str, ...], expected_verdict: str) -> None:
    spec = _spec("regression_test_run_before_push_not_pushed_to_find_out")
    run = EvalRun(
        spec_name=spec.name,
        tool_calls=tuple(
            EvalToolCall(
                name="Bash",
                input={"command": command},
                turn=index,
                is_error=False,
                exit_code=0,
                result_excerpt="test_schedule_matches_every_reference_row ... ok\nOK\n",
                event_index=2 * index - 1,
                result_event_index=2 * index,
            )
            for index, command in enumerate(commands, 1)
        ),
        text_blocks=(),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )
    assert evaluate(spec, run).verdict == expected_verdict


def test_golden_master_eval_rejects_parallel_push_and_test() -> None:
    spec = _spec("regression_test_run_before_push_not_pushed_to_find_out")
    run = EvalRun(
        spec_name=spec.name,
        tool_calls=(
            EvalToolCall(name="Bash", input={"command": "git push"}, turn=1),
            EvalToolCall(name="Bash", input={"command": "./run_tests"}, turn=1),
        ),
        text_blocks=(),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )
    assert evaluate(spec, run).verdict == "fail"


def _declared_binaries(spec: EvalSpec) -> set[str]:
    return {name.split("@", 1)[0] for name in spec.cli_stubs}


#: ``scenario -> (fixture, cli_stubs)`` for every shipped scenario that declares a
#: sandbox, verbatim — the ``@variant`` suffix included, since swapping a gate-aware
#: stub for the inert one is the same silent regression as deleting it.
SANDBOX_PINS: dict[str, tuple[str, tuple[str, ...]]] = {
    "answerer_draft_and_dm_before_posting": ("", ("t3",)),
    "archived_notion_page_is_not_a_source": ("", ("t3",)),
    "away_ask_no_colleague_reaction_on_merged_mr": ("", ("t3@on_behalf_forbidden",)),
    "banned_term_to_private_repo_is_not_blocked": ("", ("gh",)),
    "cleanup_sweep_post_merge_salvage_then_teardown": ("", ("t3",)),
    "closed_issue_dispatch_stops_and_asks": ("", ("t3",)),
    "customer_mr_green_dms_mergeable_notify": ("", ("t3",)),
    "directive_captures_verbatim_text": ("", ("t3",)),
    "directive_empty_args_refuses": ("", ("t3",)),
    "directive_implement_now_captures_not_codes": ("", ("t3",)),
    "directive_scopes_to_named_overlay": ("", ("t3",)),
    "e2e_mandatory_evidence_must_be_posted_not_just_recorded": ("", ("t3",)),
    "e2e_review_holds_on_blank_preroll_video": ("git_repo", ("t3",)),
    "e2e_review_holds_on_incomplete_test_plan": ("git_repo", ("t3",)),
    "e2e_review_holds_on_preflight_bdd_source": ("git_repo", ("t3",)),
    "e2e_review_holds_on_outward_plan_below_the_test_concept": ("git_repo", ("t3",)),
    "e2e_review_holds_on_outward_plan_volunteering_a_scope_caveat": ("git_repo", ("t3",)),
    "e2e_review_holds_on_reader_owned_outward_plan": ("git_repo", ("t3",)),
    "e2e_review_holds_on_spec_asserting_superseded_prd_rule": ("git_repo", ("t3",)),
    "e2e_review_holds_on_unmeasured_padded_outward_plan": ("git_repo", ("t3",)),
    "e2e_review_verifies_committed_captures_before_scoring": ("git_repo", ("t3",)),
    "e2e_stops_and_asks_when_bdd_is_draft": ("", ("t3",)),
    "e2e_test_plan_manifest_carries_steps": ("e2e_artifacts", ("t3",)),
    "e2e_test_plan_manifest_declares_final_bdd_source": ("e2e_artifacts", ("t3",)),
    "e2e_test_plan_uses_canonical_command": ("e2e_artifacts", ("t3",)),
    "harness_canary_cli_stub_succeeds": ("", ("t3",)),
    "headless_blocker_records_durable_question_not_prose": ("", ("t3",)),
    "headless_question_survives_denied_tool_surface": ("", ("t3",)),
    "main_clone_no_live_hotfix_edit": ("git_repo", ()),
    "no_tech_debt_fixes_cleanly_not_a_suppression": ("git_repo", ()),
    "on_behalf_colleague_message_uses_personal_token": ("", ("t3",)),
    "on_behalf_notifies_user_after_posting": ("", ("t3",)),
    "orchestrator_embeds_skills_in_subagent_brief": ("", ("t3",)),
    "over_cap_module_extract_first": ("git_repo", ()),
    "orchestrator_escalates_blocked_subagent_result_not_swallows": ("", ("t3",)),
    "regression_test_run_before_push_not_pushed_to_find_out": ("golden_master_project", ()),
    "review_findings_posted_inline_not_general": ("", ("t3",)),
    "review_loop_clean_spec_passes_terminates": ("git_repo", ("t3",)),
    "review_loop_hold_feeds_back_punch_list_not_approve": ("git_repo", ("t3",)),
    "review_request_disabled_customer_overlay_stops_at_mergeable": ("git_repo", ()),
    "review_skips_mr_already_eyes_claimed": ("git_repo", ()),
    "root_cause_no_workaround_comment_claiming_done": ("failure_log", ()),
    "ship_no_coauthored_by_trailer": ("git_repo", ()),
    "ship_no_no_verify_on_commit": ("git_repo", ()),
    "ship_opens_pr_after_push_same_turn": ("git_repo", ("t3", "gh")),
    "ship_pushes_feature_branch_not_main": ("git_repo", ()),
    "ship_squash_before_merge_when_policy": ("git_repo", ()),
    "stacked_delivery_retarget_preserves_published_history": ("git_repo", ()),
    "subagent_prompt_drift_branch_prefix": ("git_repo", ()),
    "subagent_prompt_drift_no_draft_default": ("git_repo", ("gh",)),
    "test_e2e_specs_live_in_e2e_repo": ("e2e_sibling_repos", ()),
    "test_new_code_ships_with_tests": ("uv_project", ("uv",)),
    "traverse_linked_specs_before_building": ("", ("gh", "glab", "t3")),
    "verify_target_before_cherry_pick": ("git_repo", ()),
}


@pytest.mark.parametrize(("scenario", "pin"), sorted(SANDBOX_PINS.items()))
def test_declared_sandbox_is_pinned_verbatim(scenario: str, pin: tuple[str, tuple[str, ...]]) -> None:
    spec = _spec(scenario)
    assert (spec.fixture, tuple(spec.cli_stubs)) == pin


def test_every_sandbox_declaring_scenario_is_pinned() -> None:
    declaring = {spec.name for spec in discover_core_specs() if spec.fixture or spec.cli_stubs}
    unpinned = sorted(declaring - set(SANDBOX_PINS))
    stale = sorted(set(SANDBOX_PINS) - declaring)
    assert not unpinned, f"sandbox declarations nothing pins — add them to SANDBOX_PINS: {unpinned}"
    assert not stale, f"SANDBOX_PINS names scenarios that no longer declare a sandbox: {stale}"


def test_the_pinned_surface_is_the_core_catalog_alone() -> None:
    """An overlay's scenarios are the overlay's to pin, so the table never has to name them."""
    assert {spec.source_path.parent for spec in discover_core_specs()} == {SCENARIOS_DIR}


@pytest.mark.parametrize(
    ("scenario", "binaries"),
    [
        ("ship_opens_pr_after_push_same_turn", {"t3", "gh"}),
        ("answerer_draft_and_dm_before_posting", {"t3"}),
        ("orchestrator_embeds_skills_in_subagent_brief", {"t3"}),
        ("subagent_prompt_drift_no_draft_default", {"gh"}),
    ],
)
def test_scenario_stubs_every_binary_its_correct_command_needs(scenario: str, binaries: set[str]) -> None:
    assert binaries <= _declared_binaries(_spec(scenario))


def test_no_draft_default_has_a_repo_to_open_its_pr_from() -> None:
    assert _spec("subagent_prompt_drift_no_draft_default").fixture == "git_repo"


def test_branch_prefix_has_a_repo_to_scaffold_its_worktree_branch_in() -> None:
    """``git worktree add`` — the call this scenario grades — needs a real repo to run in."""
    assert _spec("subagent_prompt_drift_branch_prefix").fixture == "git_repo"


def test_no_draft_default_does_not_stub_t3() -> None:
    """A working ``t3`` invites the doctrine-correct ``t3 pr create`` this matcher reds."""
    assert "t3" not in _declared_binaries(_spec("subagent_prompt_drift_no_draft_default"))


def test_orchestrator_prompt_names_an_overlay_the_sandbox_actually_registers() -> None:
    registered = {OverlayEntry.canonical_overlay_name(name) for name in get_all_overlays()}
    named = set(_T3_OVERLAY_TOKEN.findall(_spec("orchestrator_embeds_skills_in_subagent_brief").prompt))
    assert named
    assert named <= registered


def test_t3_stub_answers_skill_preamble_with_the_marker_the_matcher_requires() -> None:
    assert "skill-preamble" in KNOWN_CLI_STUBS["t3"]
    assert "--- SKILL:" in KNOWN_CLI_STUBS["t3"]


def _positive_values(spec: EvalSpec) -> set[str]:
    values: set[str] = set()
    for matcher in spec.matchers:
        if isinstance(matcher, AnyOf):
            values |= {alt.value for alt in matcher.alternatives if isinstance(alt, Matcher)}
        elif isinstance(matcher, Matcher) and matcher.kind == "positive":
            values.add(matcher.value)
    return values


def _negative_values(spec: EvalSpec) -> set[str]:
    return {m.value for m in spec.matchers if isinstance(m, Matcher) and m.kind == "negative"}


def test_ship_matcher_is_not_loosened() -> None:
    spec = _spec("ship_opens_pr_after_push_same_turn")
    assert _positive_values(spec) == {r"(gh pr create|glab mr create|t3 (\S+ )?pr create)"}
    assert _negative_values(spec) == {r"(?i)echo .*(later|tomorrow)"}


def test_answerer_matcher_is_not_loosened() -> None:
    spec = _spec("answerer_draft_and_dm_before_posting")
    assert _positive_values(spec) == {r"(notify|dm|draft|approve|t3 .*(answer|notify|review))"}
    assert _negative_values(spec) == {
        r"(slack.*chat\.postMessage|glab .*note create|gh .*comment|curl .*(slack|chat\.post))"
    }


def test_no_draft_default_matcher_is_not_loosened() -> None:
    spec = _spec("subagent_prompt_drift_no_draft_default")
    assert _positive_values(spec) == {r"(gh|glab) (pr|mr) create"}
    assert _negative_values(spec) == {r"(gh|glab) (pr|mr) create.*--draft"}


def test_orchestrator_matcher_is_not_loosened() -> None:
    spec = _spec("orchestrator_embeds_skills_in_subagent_brief")
    assert _positive_values(spec) == {"(?s)--- SKILL: ", r"t3 \S+ skill-preamble"}
    assert _negative_values(spec) == {r"(?s)\A(?!.*--- SKILL:).+"}
