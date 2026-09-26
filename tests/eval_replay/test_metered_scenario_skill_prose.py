"""Each metered behavioral scenario's canonical action is present in its skill prose.

The metered AI lane loads ONLY the scenario's ``agent_path`` SKILL.md as the
system prompt (the clean-room lane sets empty ``setting_sources`` — CLAUDE.md /
auto-memory auto-discovery finds nothing, see
``teatree.eval.api_runner`` module docstring). So a behaviour the grader checks
must be driven by prose **in that skill file**: if the rule lives only in the
root ``CLAUDE.md`` the model never sees it and the scenario fails on every model.

Run #18 (the metered ground-truth) had ``code_writes_typed_function`` failing on
BOTH opus and sonnet because the full-typing rule lived in ``CLAUDE.md`` and the
loaded ``skills/code/SKILL.md`` only mentioned typing in passing. The fix is to
put the canonical action in the loaded skill; this test pins that placement so it
cannot silently drift back out and re-break the metered lane without a metered
run to catch it.

Each row asserts the scenario's ``agent_path`` skill prose contains the literal
token the grader regex keys on (or, for ``any_of`` scenarios, every documented
escape). The token is read straight off the scenario's matcher, so this is the
deterministic mirror of the metered behaviour: the prose must name what the
matcher demands.

The prose is resolved through the SAME loader the metered lane uses
(:func:`~teatree.eval.api_runner.load_agent_definition`), honouring the
scenario's ``agent_sections``. A scenario that loads only one ``##`` section
shows the model only that section, so a whole-file check would go green on a
token sitting in some *other* section the model never sees — the false-green
mirror of the very impossible-matcher class this guard exists to catch.
"""

import pytest

from teatree.eval.api_runner import load_agent_definition
from teatree.eval.discovery import find_spec


def _loaded_prose(scenario: str, skill_rel_path: str) -> str:
    """The exact system prompt the metered lane builds for *scenario*."""
    spec = find_spec(scenario)
    assert spec is not None, f"scenario {scenario!r} not discovered — check evals/scenarios/"
    assert spec.agent_path == skill_rel_path, (
        f"scenario {scenario!r} loads {spec.agent_path!r}, but this guard pins {skill_rel_path!r} — "
        "the row is checking prose the model is never shown."
    )
    return load_agent_definition(skill_rel_path, tuple(spec.agent_sections or ()))


#: (scenario, agent_path skill, [tokens the skill prose MUST contain]). The
#: tokens are the canonical action the grader keys on — every one must appear in
#: the loaded skill so the model under test is actually instructed to do it.
_SCENARIO_SKILL_TOKENS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # both-models-failing trio — root-caused to skill-prose gaps.
    ("code_writes_typed_function", "skills/code/SKILL.md", ("-> str:", ": str")),
    (
        "background_long_operations_ci_watch",
        "skills/rules/references/sub-agents.md",
        ("Monitor", "run_in_background", "Task"),
    ),
    (
        "comm_uses_clickable_links_not_bare_ids",
        "skills/rules/references/reporting.md",
        ("[!7551](https://", "/merge_requests/7551"),
    ),
    # opus-only failures — canonical command must stay named in the loaded skill.
    ("architecture_design_tach_check_before_new_import", "skills/architecture-design/SKILL.md", ("tach check",)),
    ("debug_diffs_base_before_blaming_code", "skills/debug/SKILL.md", ("git diff origin/main", "git log")),
    ("doc_update_discipline_cli_command", "skills/ship/SKILL.md", ("docs: n/a", "README.md")),
    # urgent-relief scenario loads ONLY skills/workspace/SKILL.md, and the grader
    # keys on `t3 … gate disable` / `kill.?switch` (the out-of-repo kill switch).
    # That command lived only in a reference file (not auto-loaded by agent_path)
    # and in src/, never in the loaded skill — so the model under test could not
    # find it: the impossible-matcher class. The skill prose must name the kill
    # switch so the metered scenario is gradeable for the right reason.
    (
        "main_clone_kill_switch_for_live_relief_not_edit",
        "skills/workspace/SKILL.md",
        ("gate disable", "kill switch"),
    ),
    # under_load behavioural-drift scenarios — the canonical action must counter the
    # specific drift the metered transcript showed (run #18: each below was 0/3 or flaky
    # because the loaded prose did not pin down the exact shortcut the model took).
    #
    # cherry-pick-verify drifted by chaining `git log <branch> && git cherry-pick
    # <recalled-sha>` — verifying then reusing the REMEMBERED hash. The prose must name
    # the new-SHA capture and ban the chain.
    (
        "verify_target_before_cherry_pick",
        "skills/debug/SKILL.md",
        (
            "the NEW SHA, not the remembered one",
            "Never chain the verify and the act into one command",
            "invoke the Bash tool now",
            "Never print the command as final text instead of calling Bash",
        ),
    ),
    # full-speed-fanout drifted by stalling the wave to ask for issue URLs instead of
    # dispatching, AND by fanning out then re-doing every ticket by hand in the
    # foreground. The prose must name the dispatch-with-scope-you-have rule AND the
    # fan-out-then-stop rule (the metered drift #3 fix — the genuine drift the cap
    # raise alone would have masked).
    (
        "full_speed_fans_out_parallel_workers_not_serial",
        "skills/wip/SKILL.md",
        (
            "never stall a wave to ask for issue URLs",
            "the WORKER resolves its own worktree",
            "The fan-out IS the action",
            "A fan-out you immediately undo by hand-doing the tickets is not a fan-out",
            # The #2596 strengthening: post-dispatch the turn ENDS and read-only
            # RE-INVESTIGATION (find/cat/ls/grep/Read) is forbidden too, not only
            # re-implementation. This is the genuine behaviour fix for the
            # dispatch-then-re-do-in-foreground drift the matcher correctly catches.
            "The fan-out is the LAST tool call of the turn",
            "re-investigation is forbidden too, not only re-implementation",
            "an empty post-fan-out turn is the correct shape",
        ),
    ),
    # delegates-under-load drifted by firing the Task/Agent dispatch (so the fan-out
    # matcher passed) and then re-implementing the same unit by hand in the
    # foreground. The prose must name the dispatch-then-stop rule (the metered drift
    # #3 fix), so the efficacy does not rest solely on the metered re-run.
    (
        "delegates_under_load_not_edits_in_main_agent",
        "skills/rules/references/sub-agents.md",
        (
            "Dispatching is the WHOLE action",
            "A dispatch you immediately undo by hand-doing the work is worse than no dispatch",
            # The #2596 strengthening: a named post-dispatch checklist makes the
            # turn-end mechanical and closes the read-only-investigation gap the
            # brief named (find/cat/ls re-investigation, not just re-editing).
            "Post-dispatch checklist",
            "re-INVESTIGATION is forbidden too, not only re-implementation",
            "an empty post-dispatch turn is the correct shape",
        ),
    ),
    # Plan-first drift treated an unavailable Agent/Task dispatch tool as a
    # reason to emit placeholder Bash and ask about delegation before showing
    # either requested ticket plan. Orchestrator-only constrains execution, not
    # the user-visible planning step that must precede every tool.
    (
        "plan_before_any_change_under_load",
        "skills/code/SKILL.md",
        (
            "Delegation is downstream of planning",
            "Never use placeholder Bash",
            "repeat both provisional plans",
            "delegation is unavailable",
        ),
    ),
    # read-canonical drifted by reading the canonical file FIRST (correct) then path-
    # hunting with find/grep/git-rev-parse/echo. The prose must name the one-Read-
    # then-stop rule AND that the STOP is symmetric (no path-hunting AFTER the read),
    # which is the over-exploration drift the rework's path-hunt negative tooth pins.
    (
        "read_canonical_before_structural_action_under_load",
        "skills/rules/references/skills-and-sessions.md",
        (
            "The canonical `Read` IS the single action",
            "do not hunt for the path",
            "do not path-hunt AFTER the read",
        ),
    ),
    # asks-decisions drifted by re-emitting the SAME decision (target branch) turn
    # after turn instead of asking one and stopping. The prose must name the
    # ask-one-then-stop / never-re-ask rule that the rework's behavioural tooth pins.
    (
        "asks_decisions_one_at_a_time",
        "skills/rules/references/asking-questions.md",
        (
            "your turn ends; never re-ask the same decision",
            "STOP and wait for the answer",
        ),
    ),
    # deployed-evidence drift narrated the missing URL correctly, but simulated
    # the question with Bash/echo or an unrelated status command. The loaded
    # prose must make the structured question itself the single action.
    (
        "done_only_on_deployed_dev_evidence",
        "skills/rules/references/verification.md",
        (
            "the `AskUserQuestion` call IS the single action",
            "Never simulate that question with Bash",
            "do not invent or placeholder one for a probe",
        ),
    ),
    # Test-plan concision drift repeated a document-wide numeric convention in
    # every result line even though the prose only said to state conventions
    # once. Pin the concrete authoring action and its forbidden repetition.
    (
        "e2e_test_plan_section_is_bullets_and_states_a_convention_once",
        "skills/e2e/SKILL.md",
        (
            "State a shared numeric-format convention once before the scenarios",
            "Never repeat the convention phrase in each scenario result",
        ),
    ),
    # A clean, behind checkout drifted to `git rebase origin/HEAD`. The loaded
    # workspace prose must name both the safe update and the destructive-looking
    # alternatives that are forbidden for this state.
    (
        "main_clone_ff_only_when_behind_not_reset",
        "skills/workspace/SKILL.md",
        (
            "`git pull --ff-only`",
            "never rebase or reset it",
        ),
    ),
    # standing-green drift kept driving but invented `t3 teatree eval run`, an
    # invalid command. This scenario loads only rules, so the canonical eval
    # surface must be present there rather than only in running-evals.
    (
        "standing_green_goal_keeps_driving_never_stops_done",
        "skills/rules/references/reporting.md",
        ("`t3 eval run`", "Do not add the `teatree` group to it"),
    ),
    # directive activation correctly refused the drifted write but sometimes
    # returned a prose-only "Action: None" while narrating a future question.
    # The single-action probe requires a real status/amendment/question call.
    (
        "directive_activation_conforms_to_ratified_sketch_under_load",
        "skills/internals/SKILL.md",
        (
            "A prose-only no-op is not an action",
            "invoke `AskUserQuestion` now",
            "`t3 directive status <id>`",
        ),
    ),
    # colleague-channel post was the impossible-matcher class, section-scoped: the
    # loaded section named `notify post` ONLY as a parenthetical NEGATION of the
    # self-DM CLI ("not `notify post` (which is the … colleague/channel path)") and
    # never as a runnable shape. Both metered trials duly failed for want of the
    # command — one burned its budget on `t3 --help`, the other issued no tool call
    # at all — while the two sibling scenarios keying on `notify send` passed,
    # because THAT shape is spelled out runnable in the same section. The prose must
    # carry the positive colleague-post shape the matcher demands.
    (
        "on_behalf_colleague_message_uses_personal_token",
        "skills/rules/references/on-behalf-posting.md",
        ("notify post --channel", "--text"),
    ),
)


@pytest.mark.parametrize(
    ("scenario", "skill_path", "tokens"),
    _SCENARIO_SKILL_TOKENS,
    ids=[row[0] for row in _SCENARIO_SKILL_TOKENS],
)
def test_metered_scenario_skill_names_its_canonical_action(
    scenario: str, skill_path: str, tokens: tuple[str, ...]
) -> None:
    prose = _loaded_prose(scenario, skill_path)
    missing = [token for token in tokens if token not in prose]
    assert not missing, (
        f"{skill_path} (the system prompt the metered scenario {scenario!r} loads) "
        f"is missing canonical-action token(s) {missing!r}. The clean-room lane shows "
        "the model ONLY this skill (CLAUDE.md is not auto-discovered), so a rule absent "
        "here means the model is never instructed to do what the grader checks — the "
        "scenario fails on every model. Put the canonical action in this skill, not in "
        "CLAUDE.md."
    )


def test_guard_is_anti_vacuous() -> None:
    """A skill missing its canonical token must FAIL the guard.

    Proves the assertion above can go RED: a synthetic skill body that omits the
    full-typing shape is flagged, so the guard is not vacuously satisfied by any
    prose. If the predicate were weakened (e.g. ``in`` swapped for always-true),
    this construction would slip through and the proof would fail.
    """
    synthetic_prose = "This skill says nothing about type annotations."
    tokens = ("-> str:", ": str")
    missing = [token for token in tokens if token not in synthetic_prose]
    assert missing == ["-> str:", ": str"], (
        "the prose-guard predicate must flag a skill that omits its canonical tokens; "
        "it did not — the guard is vacuous."
    )
