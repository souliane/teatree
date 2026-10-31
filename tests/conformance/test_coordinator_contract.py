"""The coordinator contract in BLUEPRINT.md: the three role words, the trigger kinds and the duty map (#5685).

Each check is a function over text, so a planted fault proves the check can fail before the real tree is trusted.
"""

import re
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
