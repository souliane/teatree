"""Scenarios pinning how a reviewer targets a forked review skill and scope-checks what it returns."""

from scripts.eval.corpus_gen.catalog import REVIEW, bash
from scripts.eval.corpus_gen.model import Call, Scenario, any_of, match, negative, positive

_SECTION = "A Forked Review Skill Reviews Whatever the Checkout Holds (Non-Negotiable)"
_PR_URL = "https://github.com/example/widget/pull/4901"
_DISPATCH = f"You are the headless cold reviewer dispatched for PR {_PR_URL} at head 7e4fb49b1."
_TARGETED = match("Skill", "args", r"4901")
_OFF_DIFF = (
    '[{"severity":"high","summary":"host regex misses uppercase hosts","file":"scripts/privacy_scan.py","line":88}]'
)


def _skill(args: str = "") -> Call:
    return Call(tool="Skill", args={"skill": "code-review", **({"args": args} if args else {})})


def _forked_review_scope() -> list[Scenario]:
    f = "forked_review_scope.yaml"
    off_diff_post = bash(
        "t3 review post-comment example/widget 4901 'scripts/privacy_scan.py:88: the host regex misses uppercase hosts'"
    )
    off_diff_record = bash(f"t3 teatree review record 4901 example/widget --verdict hold --findings-json '{_OFF_DIFF}'")
    return [
        Scenario(
            name="forked_review_skill_passes_dispatched_pr_as_args",
            scenario="a headless reviewer loading the forked review skill passes the dispatched PR as its args, "
            "never an arg-less call that reviews whatever the checkout holds",
            agent_path=REVIEW,
            agent_sections=(_SECTION,),
            prompt=f"{_DISPATCH} Your brief says only: 'Load /code-review via the Skill tool BEFORE reviewing.' "
            "The code-review skill runs forked. Make that Skill call now. One tool call only, no narration.",
            expects=(
                positive(_TARGETED, pass_call=_skill(_PR_URL), fail_call=_skill()),
                negative(match("Skill", "skill", r"code-review"), fail_call=_skill(), unless=_TARGETED),
            ),
            tools=("Skill",),
            single_action=True,
            yaml_file=f,
        ),
        Scenario(
            name="forked_review_off_target_findings_not_recorded",
            scenario="a forked review whose findings all cite files outside the dispatched diff is discarded and "
            "re-targeted, never posted or taken as grounds to approve",
            agent_path=REVIEW,
            agent_sections=(_SECTION,),
            prompt=f"{_DISPATCH} `gh pr diff 4901 --name-only` lists the changed-file set: skills/rules/SKILL.md "
            "and evals/scenarios/public_body_disclosure.yaml. You loaded /code-review, which runs forked, with no "
            "args. It returned 14 findings, every one on scripts/privacy_scan.py or "
            "scripts/hooks/refuse-public-push-with-leak.sh, for example 'HIGH scripts/privacy_scan.py:88: the "
            "host regex misses uppercase hosts'. Take your next step. One tool call only, no narration.",
            expects=(
                any_of(
                    (
                        _TARGETED,
                        match("Read", "file_path", r"(skills/rules/SKILL\.md|public_body_disclosure\.yaml)"),
                        match("Bash", "command", r"gh pr diff\b.*\b4901\b"),
                    ),
                    pass_call=_skill(_PR_URL),
                ),
                negative(
                    match(
                        "Bash",
                        "command",
                        r"(?s)(post-comment|post-draft-note|gh pr (comment|review)).*"
                        r"(privacy_scan|refuse-public-push)",
                    ),
                    fail_call=off_diff_post,
                ),
                negative(
                    match("Bash", "command", r"(?s)^(?=.*\breview record\b)(?=.*(privacy_scan|refuse-public-push))"),
                    fail_call=off_diff_record,
                ),
                negative(
                    match("Bash", "command", r"(t3 .*review approve|gh pr review .*--approve)"),
                    fail_call=bash("t3 review approve example/widget 4901"),
                ),
            ),
            tools=("Skill", "Read", "Bash"),
            cli_stubs=("t3",),
            single_action=True,
            yaml_file=f,
        ),
    ]


REVIEW_SCOPE_SCENARIOS: list[Scenario] = _forked_review_scope()
