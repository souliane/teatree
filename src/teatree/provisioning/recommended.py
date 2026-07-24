"""Curated OPTIONAL vendor skills teatree RECOMMENDS but never mandates (#3668).

Distinct from :mod:`teatree.provisioning.declared`, whose ``apm.yml`` surface
MANDATES a skill and makes its absence a ``t3 doctor`` FAIL. A recommendation is
provider- or context-specific guidance an operator MAY install; its absence is a
doctor INFO, never a gate, and it is NOT installed by default.

The first entry is the agent-platform vendor's architecture skill. Its guidance
is load-bearing on Claude/Anthropic API primitives (adaptive thinking, effort,
programmatic tool calling, context editing, prompt-caching breakpoints, the
Managed Agents API), and its own frontmatter directs the reader to SKIP it for
non-Claude providers — so it is offered only to operators running on that
provider, with an explicit caveat, rather than shipped by default.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from teatree.provisioning.probes import skill_is_provisioned


@dataclass(frozen=True, slots=True)
class RecommendedSkill:
    """One OPTIONAL skill teatree suggests, with why it is optional and its scope."""

    name: str
    #: The ``apm`` install spec ``<owner>/<repo>/<subpath>#<ref>`` — ref pinned.
    source: str
    caveat: str
    rationale: str
    overlap_note: str

    @property
    def install_hint(self) -> str:
        return f"apm install {self.source}"


_VENDOR_ARCHITECTURE_SKILL = RecommendedSkill(
    name="claude-api",
    source="anthropics/skills/skills/claude-api#1f630fdf9259cec4a14913127dfd7c3b69ef72eb",
    caveat=(
        "Anthropic-specific — its agent-design and managed-agents guidance is load-bearing on "
        "Claude/Anthropic API primitives (adaptive thinking, effort, programmatic tool calling, "
        "context editing, prompt-caching breakpoints, the Managed Agents API), and its own frontmatter "
        "directs the reader to SKIP it for non-Claude providers. Install it only if you run teatree on Claude."
    ),
    rationale=(
        "Vendor architecture decision-trees (single call vs workflow vs agent), tool-surface orchestration, "
        "agent tiering, and resiliency patterns (error handling, session lifecycle)."
    ),
    overlap_note=(
        "Complements the local `architecture-design` skill: local is scoped to THIS repo's conventions "
        "(BLUEPRINT alignment, FSM phase boundaries, extension-point contracts); the vendor skill covers "
        "the layer beneath — how to structure agent work at all. Local = repo conventions; vendor = "
        "provider-level agent architecture."
    ),
)

RECOMMENDED_SKILLS: tuple[RecommendedSkill, ...] = (_VENDOR_ARCHITECTURE_SKILL,)


def unprovisioned_recommendations(
    search_dirs: Sequence[Path],
    recommendations: Sequence[RecommendedSkill] = RECOMMENDED_SKILLS,
) -> list[RecommendedSkill]:
    """The subset of *recommendations* not already loadable on *search_dirs*."""
    return [rec for rec in recommendations if not skill_is_provisioned(rec.name, search_dirs)]
