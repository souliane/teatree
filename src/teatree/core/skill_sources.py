"""The overlays' declared skill sources, read once for both the gate and the install.

The declaration lives on the overlay (``OverlayConfig.skill_source_clones``), which
is domain state; acting on it — measuring drift, installing what it publishes — is
platform work. This module is the one place the two meet, so an overlay that
declares a source gets it BOTH measured and provisioned, with no second list to keep
in step. Splitting them is what let an overlay gate on skills its own provisioning
never installed.
"""

from pathlib import Path

from teatree.core.modelkit.phases import SUBAGENT_BY_PHASE
from teatree.core.overlay_loader import get_all_overlays
from teatree.provisioning.skill_clone_install import CloneInstall, install_published_skills
from teatree.provisioning.skill_drift import SkillSourceClone
from teatree.provisioning.skills_cli import SkillsCli
from teatree.skill_support.agent_declarations import declared_skills_for_agent
from teatree.skill_support.demands import SkillDemand, enumerate_skill_demands, skill_demand_names
from teatree.skill_support.loading import SkillLoadingPolicy


def declared_skill_sources() -> list[SkillSourceClone]:
    """Every registered overlay's declared skill-source clones, deduped."""
    sources: dict[tuple[str, tuple[str, ...], str], SkillSourceClone] = {}
    for overlay in get_all_overlays().values():
        config = getattr(overlay, "config", None)
        for clone in getattr(config, "skill_source_clones", []) or []:
            sources[clone.label, tuple(clone.paths), clone.ref] = clone
    return list(sources.values())


def skill_demands_by_overlay() -> dict[str, tuple[SkillDemand, ...]]:
    from teatree.config import get_effective_settings  # noqa: PLC0415 — avoids config↔skill-source import cycle

    demands: dict[str, tuple[SkillDemand, ...]] = {}
    for overlay_name, overlay in get_all_overlays().items():
        config = getattr(overlay, "config", None)
        if config is not None:
            demands[overlay_name] = enumerate_skill_demands(config, get_effective_settings(overlay_name))
    return demands


def demanded_skill_names() -> tuple[str, ...]:
    demands = [demand for overlay_demands in skill_demands_by_overlay().values() for demand in overlay_demands]
    phase_agents = {
        phase: agent.removeprefix("t3:")
        for (_role, phase), agent in SUBAGENT_BY_PHASE.items()
        if agent.startswith("t3:")
    }
    for phase, agent in sorted(phase_agents.items()):
        agent_skills = declared_skills_for_agent(agent)
        required = agent_skills or [SkillLoadingPolicy.lifecycle_for_phase(phase)]
        demands.extend(SkillDemand(f"agent[{phase}]", skill) for skill in required if skill)
    return skill_demand_names(demands)


def install_declared_sources(
    *,
    cache_root: Path,
    demand_names: set[str],
    harness_exclusions: list[str],
    cli: SkillsCli | None = None,
) -> list[CloneInstall]:
    return [
        install_published_skills(
            clone,
            cache_root=cache_root,
            demand_names=demand_names,
            harness_exclusions=harness_exclusions,
            cli=cli,
        )
        for clone in declared_skill_sources()
    ]
