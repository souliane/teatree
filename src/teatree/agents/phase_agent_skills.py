"""Phase → dispatched agent file → declared skills (#3667).

The bridge between the domain dispatch table
(:data:`~teatree.core.modelkit.phases.SUBAGENT_BY_PHASE`) and the platform-layer
frontmatter reader (:mod:`teatree.skill_support.agent_declarations`), so a
headless dispatch loads the SAME skills the interactive lane resolves from the
agent file rather than a parallel one-skill-per-phase table.

Resolution is role-agnostic on purpose: no phase dispatches two DIFFERENT
``t3:`` agents across roles (a phase registered for both roles names one agent),
so the phase alone identifies the declaration. That is pinned by
``test_phase_agent_skills.TestOneAgentPerPhase`` rather than assumed, and it
keeps the skill bundle keyed on the one thing every caller already has.
"""

from pathlib import Path

from teatree.core.modelkit.phases import SUBAGENT_BY_PHASE, normalize_phase
from teatree.skill_support.agent_declarations import declared_skills_for_agent

#: Namespace prefix of a teatree ``agents/<name>.md`` sub-agent. A dispatch row in
#: another namespace (``codex:``) names a slash-command agent with no agent file.
_AGENT_NAMESPACE = "t3:"


def agent_names_for_phase(phase: str) -> set[str]:
    """Every ``agents/<name>.md`` stem any role dispatches for *phase*."""
    canonical = normalize_phase(phase)
    return {
        agent.removeprefix(_AGENT_NAMESPACE)
        for (_role, registered), agent in SUBAGENT_BY_PHASE.items()
        if registered == canonical and agent.startswith(_AGENT_NAMESPACE)
    }


def agent_file_name_for_phase(phase: str) -> str:
    """The single ``agents/<name>.md`` stem dispatched for *phase*, or ``""``."""
    names = agent_names_for_phase(phase)
    return next(iter(names)) if len(names) == 1 else ""


def declared_skills_for_phase(phase: str, *, agents_dir: Path | None = None) -> list[str]:
    """The skills *phase*'s agent file declares, or ``[]`` when it has none."""
    name = agent_file_name_for_phase(phase)
    if not name:
        return []
    return declared_skills_for_agent(name, agents_dir=agents_dir)


__all__ = ["agent_file_name_for_phase", "agent_names_for_phase", "declared_skills_for_phase"]
