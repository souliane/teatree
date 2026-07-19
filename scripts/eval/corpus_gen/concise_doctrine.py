"""Directive #4 (concise everywhere) behavioral scenario.

Covers the RESIDUAL surface the per-surface gates do not: authored MR/commit
BODY prose. The review shape/bloat gates, the Slack mrkdwn path, and the MR-title
eval already grade their own surfaces; nothing graded that an authored PR/MR body
is bullets, not an essay. This scenario does — it asserts the body carries bullet
findings and is free of prose-filler wind-up. Aggregated into ``ALL_SCENARIOS``
by :mod:`scripts.eval.corpus_gen.all_scenarios`; kept in its own module so the
over-cap god-files stay untouched.
"""

from scripts.eval.corpus_gen.catalog import SHIP
from scripts.eval.corpus_gen.model import Call, Scenario, match, negative, positive

_YAML = "concise_authored_output.yaml"

# A bullet line anywhere in the body (``-``/``*`` list marker at line start).
_BULLETS = r"(?m)^\s*[-*] "

# Prose-filler wind-up openers a concise body never uses.
_PROSE_FILLER = (
    r"(?i)(this (pr|mr|change) (aims|introduces|is designed|seeks)"
    r"|as we can see|it is worth noting that"
    r"|the purpose of this (pr|mr|change) is|in this (pr|mr) we)"
)

_BULLET_BODY = (
    "fix(loop): guard empty owner in admission\n\n"
    "- what: reject a None owner before the mini-loop reads it\n"
    "- why: a None owner crashed admission (#123)\n"
    "- test: regression in test_loop_admission\n"
)

_PROSE_BODY = (
    "This change fixes a problem in the loop admission path. Previously the code "
    "did not guard against an empty owner value, which meant that when None was "
    "passed in the mini-loop would crash at read time. We now handle this case "
    "gracefully and there is a regression test covering it.\n"
)

_FILLER_BODY = (
    "This PR introduces a guard for empty owner values in the loop admission path. "
    "As we can see, the previous behaviour was to crash. The purpose of this change "
    "is to handle the empty owner gracefully so the mini-loop stays up.\n"
)


def _write_body(content: str) -> Call:
    return Call(tool="Write", args={"file_path": "pr-body.md", "content": content})


CONCISE_DOCTRINE: list[Scenario] = [
    Scenario(
        name="authored_mr_body_is_bullets_not_prose",
        scenario="an authored PR/MR body is terse bullet points, not a multi-paragraph prose essay",
        agent_path=SHIP,
        prompt=(
            "You just shipped a two-line fix that guards an empty owner value in the loop "
            "admission path (issue #123, with a regression test). Write the PR/MR description "
            "body to pr-body.md exactly as you would ship it. This is a sandboxed exercise — do "
            "NOT inspect the filesystem or verify anything first; author the body and write the "
            "file in a single action, no narration."
        ),
        tools=("Write",),
        expects=(
            positive(
                match("Write", "content", _BULLETS),
                pass_call=_write_body(_BULLET_BODY),
                fail_call=_write_body(_PROSE_BODY),
            ),
            negative(
                match("Write", "content", _PROSE_FILLER),
                fail_call=_write_body(_FILLER_BODY),
            ),
        ),
        yaml_file=_YAML,
    ),
]
