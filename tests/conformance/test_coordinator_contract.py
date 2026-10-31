"""The coordinator contract in BLUEPRINT.md: the three role words, the trigger kinds and the duty map (#5685).

Each check is a function over text, so a planted fault proves the check can fail before the real tree is trusted.
"""

import re
from collections.abc import Mapping
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_BLUEPRINT = (_ROOT / "BLUEPRINT.md").read_text(encoding="utf-8")

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
