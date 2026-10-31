"""The coordinator contract in BLUEPRINT.md: the three role words, the trigger kinds and the duty map (#5685).

Each check is a function over text, so a planted fault proves the check can fail before the real tree is trusted.
"""

import re
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path

from teatree.core.modelkit.coordinator_action import ACTION_SPECS, CoordinatorActionKind

_ROOT = Path(__file__).resolve().parents[2]
_BLUEPRINT = (_ROOT / "BLUEPRINT.md").read_text(encoding="utf-8")
_SKILL_FILES = {skill: _ROOT / "skills" / skill / "SKILL.md" for skill in ("coordinating", "interactive")}

_DEFINITIONS: dict[str, tuple[str, ...]] = {
    "Interactive session": ("attended", "interactive skill"),
    "Orchestrator": ("deterministic control plane", "code, never an agent"),
    "Coordinator": ("deciding agent role", "Slack is one entry point", "mailbox"),
}
_CONFLATIONS: dict[str, tuple[str, ...]] = {
    "Interactive session": ("deterministic control plane", "deciding agent role"),
    "Orchestrator": ("deciding agent role", "attended"),
    "Coordinator": ("code, never an agent", "attended"),
}


_TRIGGER_STATUS: Mapping[str, str] = {
    "owner message": "built",
    "agent message": "built",
    "factory event": "later",
    "schedule": "later",
}
_TRIGGERS_LEAD = "**Coordinator triggers.**"
_DUTY_MAP_LEAD = "**Coordinator duty map.**"
_OPEN_POINTS_LEAD = "**Open points.**"
_OPEN_POINTS = frozenset({"O1", "O2", "O3", "O4", "O5", "O6"})
_ATTENDED_ONLY = "attended-only"
_NOT_APPLICABLE = "n/a"


def section_one(blueprint: str) -> str:
    return blueprint.split("\n## 1. ", 1)[1].split("\n## 2. ", 1)[0]


def glossary_problems(blueprint: str) -> list[str]:
    section = section_one(blueprint)
    problems = [] if "not synonyms" in section else ["section 1 never says the three words are not synonyms"]
    for term, required in _DEFINITIONS.items():
        found = re.search(rf"^\*\*{term}\.\*\* (.+)$", section, flags=re.MULTILINE)
        if not found:
            problems.append(f"{term}: no definition in section 1")
            continue
        problems += [f"{term}: does not say {phrase!r}" for phrase in required if phrase not in found[1]]
        problems += [f"{term}: also says {phrase!r}" for phrase in _CONFLATIONS[term] if phrase in found[1]]
    return problems


def table_after(blueprint: str, lead: str) -> list[list[str]]:
    """The data rows of the first markdown table after the paragraph that starts with *lead*."""
    lines = blueprint.split("\n")
    start = next((i for i, line in enumerate(lines) if line.startswith(lead)), None)
    if start is None:
        return []
    rows: list[list[str]] = []
    for line in lines[start + 1 :]:
        if line.startswith("|"):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
        elif rows:
            break
    return rows[2:]


def trigger_problems(blueprint: str) -> list[str]:
    paragraph = next((line for line in blueprint.split("\n") if line.startswith(_TRIGGERS_LEAD)), "")
    status = {cells[0]: cells[1] for cells in table_after(blueprint, _TRIGGERS_LEAD)}
    problems = [
        f"{kind}: expected {want!r}, found {status.get(kind)!r}"
        for kind, want in _TRIGGER_STATUS.items()
        if status.get(kind) != want
    ]
    problems += [f"unexpected trigger kind {kind!r}" for kind in status.keys() - _TRIGGER_STATUS.keys()]
    problems += [
        f"trigger paragraph does not say {phrase!r}"
        for phrase in ("information, not an instruction", "issues/5816", "L1", "L2", "L5")
        if phrase not in paragraph
    ]
    return problems


def second_level_headings(markdown: str) -> list[str]:
    headings: list[str] = []
    fenced = False
    for line in markdown.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("## "):
            headings.append(line.removeprefix("## "))
    return headings


def duty_map_problems(
    blueprint: str,
    headings: Mapping[str, Sequence[str]],
    kinds: Collection[str],
    duties: Mapping[str, str],
) -> list[str]:
    rows = [
        (skill, heading, [token.strip("` ") for token in carried.split(",")])
        for skill, heading, carried in table_after(blueprint, _DUTY_MAP_LEAD)
    ]
    defined = {cells[0] for cells in table_after(blueprint, _OPEN_POINTS_LEAD)}
    listed = Counter((skill, heading) for skill, heading, _tokens in rows)
    real = {(skill, heading) for skill, skill_headings in headings.items() for heading in skill_headings}
    problems = [f"open point {point} is not defined" for point in sorted(_OPEN_POINTS - defined)]
    problems += [
        f"{skill}: {heading!r} is listed {listed[skill, heading]} times"
        for skill, heading in sorted(real)
        if listed[skill, heading] != 1
    ]
    problems += [
        f"{skill}: {heading!r} is not a heading of that skill" for skill, heading in sorted(listed.keys() - real)
    ]
    for skill, heading, tokens in rows:
        markers = {_ATTENDED_ONLY, _NOT_APPLICABLE} & set(tokens)
        if markers and len(tokens) > 1:
            problems.append(f"{heading!r}: {sorted(markers)} cannot be combined with another value")
        if skill == "coordinating" and _ATTENDED_ONLY in tokens:
            problems.append(f"{heading!r}: a coordinating duty is never attended-only")
        for token in tokens:
            if token in {_ATTENDED_ONLY, _NOT_APPLICABLE}:
                continue
            if re.fullmatch(r"O\d+", token):
                if token not in defined:
                    problems.append(f"{heading!r}: open point {token} is not defined")
            elif token not in kinds:
                problems.append(f"{heading!r}: {token!r} is not an action kind")
            elif skill == "coordinating" and duties.get(token) != heading:
                problems.append(f"{token}: listed under {heading!r} but its duty is {duties.get(token)!r}")
    carried = {(skill, heading): tokens for skill, heading, tokens in rows}
    problems += [
        f"{kind}: the row for {duty!r} does not carry it"
        for kind, duty in duties.items()
        if kind not in carried.get(("coordinating", duty), ())
    ]
    return problems


def real_duty_map_problems(blueprint: str) -> list[str]:
    return duty_map_problems(
        blueprint,
        {skill: second_level_headings(path.read_text(encoding="utf-8")) for skill, path in _SKILL_FILES.items()},
        {kind.value for kind in CoordinatorActionKind},
        {kind.value: spec.duty for kind, spec in ACTION_SPECS.items()},
    )


class TestTheThreeRoleWordsAreDefined:
    def test_section_one_defines_interactive_session_orchestrator_and_coordinator(self) -> None:
        assert glossary_problems(_BLUEPRINT) == []

    def test_a_missing_definition_is_reported(self) -> None:
        without = re.sub(r"(?m)^\*\*Orchestrator\.\*\* .+$", "", _BLUEPRINT)

        assert "Orchestrator: no definition in section 1" in glossary_problems(without)

    def test_an_orchestrator_that_may_be_an_agent_is_reported(self) -> None:
        agentive = _BLUEPRINT.replace("code, never an agent", "code or an agent")

        assert "Orchestrator: does not say 'code, never an agent'" in glossary_problems(agentive)

    def test_an_orchestrator_defined_as_the_deciding_role_is_reported(self) -> None:
        merged = re.sub(r"(?m)^(\*\*Orchestrator\.\*\* .+)$", r"\1 It is the deciding agent role.", _BLUEPRINT)

        assert "Orchestrator: also says 'deciding agent role'" in glossary_problems(merged)

    def test_a_coordinator_that_treats_slack_as_its_identity_is_reported(self) -> None:
        slack_only = _BLUEPRINT.replace("Slack is one entry point", "Slack is the only way in")

        assert "Coordinator: does not say 'Slack is one entry point'" in glossary_problems(slack_only)


class TestTheTriggerKindsAreNamedWithTheirStatus:
    def test_four_kinds_are_named_and_the_first_two_are_built(self) -> None:
        assert trigger_problems(_BLUEPRINT) == []

    def test_a_fifth_trigger_kind_is_reported(self) -> None:
        extra = _BLUEPRINT.replace("| schedule | later |", "| webhook | built |\n| schedule | later |")

        assert "unexpected trigger kind 'webhook'" in trigger_problems(extra)

    def test_a_built_claim_for_a_trigger_that_is_not_built_is_reported(self) -> None:
        overclaimed = _BLUEPRINT.replace("| factory event | later |", "| factory event | built |")

        assert "factory event: expected 'later', found 'built'" in trigger_problems(overclaimed)

    def test_a_dropped_trigger_kind_is_reported(self) -> None:
        dropped = re.sub(r"(?m)^\| schedule \|.*\n", "", _BLUEPRINT)

        assert "schedule: expected 'later', found None" in trigger_problems(dropped)

    def test_a_peer_message_read_as_an_instruction_is_reported(self) -> None:
        obeyed = _BLUEPRINT.replace("information, not an instruction", "an instruction")

        assert "trigger paragraph does not say 'information, not an instruction'" in trigger_problems(obeyed)


class TestEverySkillSectionHasAPlaceInTheDutyMap:
    def test_every_coordinating_and_interactive_section_is_mapped_once_to_a_real_target(self) -> None:
        assert real_duty_map_problems(_BLUEPRINT) == []

    def test_an_unmapped_section_is_reported(self) -> None:
        for skill, path in _SKILL_FILES.items():
            grown = path.read_text(encoding="utf-8") + "\n## A section nobody mapped\n"
            headings = {s: second_level_headings(f.read_text(encoding="utf-8")) for s, f in _SKILL_FILES.items()}
            headings[skill] = second_level_headings(grown)

            problems = duty_map_problems(_BLUEPRINT, headings, {k.value for k in CoordinatorActionKind}, {})

            assert f"{skill}: 'A section nobody mapped' is listed 0 times" in problems

    def test_a_heading_inside_a_code_fence_is_not_a_section(self) -> None:
        assert second_level_headings("## Real\n```\n## Quoted\n```\n## Also real") == ["Real", "Also real"]

    def test_a_section_listed_twice_is_reported(self) -> None:
        row = "| coordinating | Mailbox, both ways | `message_peer` |"
        doubled = _BLUEPRINT.replace(row, f"{row}\n{row}")

        assert "coordinating: 'Mailbox, both ways' is listed 2 times" in real_duty_map_problems(doubled)

    def test_a_row_for_a_section_that_does_not_exist_is_reported(self) -> None:
        row = "| coordinating | Mailbox, both ways | `message_peer` |"
        renamed = _BLUEPRINT.replace(row, row.replace("Mailbox, both ways", "Mailbox, one way"))

        assert "coordinating: 'Mailbox, one way' is not a heading of that skill" in real_duty_map_problems(renamed)

    def test_a_mapped_kind_that_does_not_exist_is_reported(self) -> None:
        row = "| coordinating | Resolve settled owner cards and agent asks | `settle_card` |"
        invented = _BLUEPRINT.replace(row, row.replace("`settle_card`", "`delete_branch`"))

        assert (
            "'Resolve settled owner cards and agent asks': 'delete_branch' is not an action kind"
            in real_duty_map_problems(invented)
        )

    def test_an_action_kind_with_no_row_is_reported(self) -> None:
        problems = duty_map_problems(
            _BLUEPRINT,
            {skill: second_level_headings(path.read_text(encoding="utf-8")) for skill, path in _SKILL_FILES.items()},
            {"fifth_kind"},
            {"fifth_kind": "Mailbox, both ways"},
        )

        assert "fifth_kind: the row for 'Mailbox, both ways' does not carry it" in problems

    def test_a_kind_listed_under_a_duty_that_is_not_its_own_is_reported(self) -> None:
        moved = _BLUEPRINT.replace(
            "`message_peer` |\n| coordinating | A status", "`message_peer`, `settle_card` |\n| coordinating | A status"
        )

        assert any(
            p.startswith("settle_card: listed under 'Mailbox, both ways'") for p in real_duty_map_problems(moved)
        )

    def test_an_open_point_used_but_not_defined_is_reported(self) -> None:
        undefined = re.sub(r"(?m)^\| O5 \|.*\n", "", _BLUEPRINT)

        assert "open point O5 is not defined" in real_duty_map_problems(undefined)

    def test_a_coordinating_duty_marked_attended_only_is_reported(self) -> None:
        wrong = _BLUEPRINT.replace(
            "| coordinating | Drain stranded work | O4 |", "| coordinating | Drain stranded work | attended-only |"
        )

        assert "'Drain stranded work': a coordinating duty is never attended-only" in real_duty_map_problems(wrong)
