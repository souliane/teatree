"""A command the shipped doctrine mandates must be ACCEPTED by the scenario grading it.

`#4137 <https://github.com/souliane/teatree/issues/4137>`_: the ship doctrine moved
to ``t3 push`` and the generated corpus kept asserting the pre-#3949 ``git push``
spelling, so an agent following the skill exactly was graded wrong on three
scenarios for four days. Nothing linked the doctrine to the matchers, so the drift
was silent until a metered run burned on it.

Each row below is that missing link, made mechanical: the command must still be
mandated by the named skill (a doctrine respelling reds the row), a positive
matcher of each named scenario must accept it (a matcher narrowing reds the row),
and no negative may forbid it (a negative widened past the mandated command reds
the row). The rows are deliberately literal — this pins the specific commands the
issue names, not a general "grep every backticked command" sweep, which would grade
prose rather than doctrine.

The mandate is checked against the SLICE the scenario grades, not the whole file.
A scenario declaring ``agent_sections`` sends only those sections to the model, so
a command sitting elsewhere in the same SKILL.md — or moved out to a
``references/`` file — is unreachable by the agent being graded even though a
whole-file read still finds it. The slice is the strictly stronger form, and it
is checked against what the runner itself loads: the slice is resolved through
the harness's own ``load_agent_definition``, so this test and the runner can
never disagree about what the agent actually saw.

The related loss on record is broader than a slice, and the distinction matters:
``t3 slack react`` left ``skills/rules/SKILL.md`` entirely
(``git show 0fd72ed5a^:skills/rules/SKILL.md`` finds no occurrence; ``0fd72ed5a``
returned it), so a whole-file assertion would have caught THAT one. The
slice-scoped loss — present in the file, absent from the graded sections — is
what this test adds; it has not happened yet, and the point is that it would be
invisible to the whole-file check if it did.
"""

import re
from pathlib import Path

import pytest

from teatree.eval.api_runner import load_agent_definition
from teatree.eval.discovery import find_spec
from teatree.eval.models import AnyOf, EvalSpec, Matcher

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: ``(skill, mandated command, scenario)`` — the skill sentence and the matcher that
#: has to agree with it. ``t3 push`` is ``skills/ship/SKILL.md`` § 4a ("the ONE
#: supported way to push from the worker container"); ``bash dev/test-affected.sh``
#: is ``skills/test/SKILL.md`` § "How it selects" ("Use ``bash dev/test-affected.sh``,
#: which force-keeps the floor").
_MANDATED: tuple[tuple[str, str, str], ...] = (
    ("skills/ship/SKILL.md", "t3 push", "ship_pushes_feature_branch_not_main"),
    ("skills/ship/SKILL.md", "t3 push", "safety_no_force_push_to_shared_branch"),
    ("skills/ship/SKILL.md", "t3 push", "container_push_uses_the_supported_seam"),
    ("skills/test/SKILL.md", "bash dev/test-affected.sh", "scoped_test_not_local_full_suite"),
    ("skills/rules/SKILL.md", "t3 slack react --channel", "approved_colleague_reaction_fires_and_dms_receipt"),
)


def _spec(name: str) -> EvalSpec:
    spec = find_spec(name)
    assert spec is not None, f"scenario {name!r} not found in the shipped catalog"
    return spec


def _bash_command_matchers(spec: EvalSpec, kind: str) -> list[Matcher]:
    flat: list[Matcher] = []
    for matcher in spec.matchers:
        flat.extend(matcher.alternatives if isinstance(matcher, AnyOf) else (matcher,))
    return [m for m in flat if m.kind == kind and m.tool == "Bash" and m.arg_path == "command"]


def _accepts(matcher: Matcher, command: str) -> bool:
    if matcher.operator == "~":
        return re.search(matcher.value, command) is not None
    return matcher.value in command


@pytest.mark.parametrize(("skill", "command", "scenario"), _MANDATED)
def test_the_graded_slice_still_mandates_the_command(skill: str, command: str, scenario: str) -> None:
    spec = _spec(scenario)
    assert spec.agent_path == skill, f"{scenario} grades {spec.agent_path}, not {skill} — the row is stale"
    graded = load_agent_definition(spec.agent_path, spec.agent_sections)
    sections = ", ".join(spec.agent_sections) or "the whole file"
    assert command in graded, (
        f"{skill} no longer names {command!r} inside the slice {scenario} grades ({sections}). "
        "Present elsewhere in the file, or in a references/ page, does not count — the agent "
        "being graded is only sent this slice."
    )


@pytest.mark.parametrize(("skill", "command", "scenario"), _MANDATED)
def test_a_positive_matcher_accepts_the_mandated_command(skill: str, command: str, scenario: str) -> None:
    positives = _bash_command_matchers(_spec(scenario), "positive")
    assert any(_accepts(m, command) for m in positives), (
        f"{scenario} grades no positive that accepts {command!r}, which {skill} mandates"
    )


@pytest.mark.parametrize(("skill", "command", "scenario"), _MANDATED)
def test_no_negative_matcher_forbids_the_mandated_command(skill: str, command: str, scenario: str) -> None:
    forbidding = [m.value for m in _bash_command_matchers(_spec(scenario), "negative") if _accepts(m, command)]
    assert not forbidding, f"{scenario} forbids {command!r}, which {skill} mandates: {forbidding}"
