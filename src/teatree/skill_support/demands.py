from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol


class OverlaySkillConfig(Protocol):
    stage_skills: Mapping[str, list[str]]
    companion_skills: list[str]
    pr_review_companion: str


class RuntimeSkillSettings(Protocol):
    review_skill: str
    review_skill_alternates: list[str]
    architectural_review_skill: str
    scanning_news_skill: str
    eval_local_skill: str
    backlog_sweep_skill: str
    dogfood_smoke_skill: str


@dataclass(frozen=True, slots=True)
class SkillDemand:
    source: str
    name: str


def _field_demands(source: str, values: Iterable[str]) -> list[SkillDemand]:
    return [SkillDemand(source, value) for value in values if value.strip()]


def enumerate_skill_demands(
    config: OverlaySkillConfig,
    settings: RuntimeSkillSettings,
) -> tuple[SkillDemand, ...]:
    demands = [
        SkillDemand(f"stage_skills[{phase}]", skill)
        for phase, skills in getattr(config, "stage_skills", {}).items()
        for skill in skills
        if skill.strip()
    ]
    demands.extend(_field_demands("companion_skills", getattr(config, "companion_skills", [])))
    if companion := getattr(config, "pr_review_companion", "").strip():
        demands.append(SkillDemand("pr_review_companion", companion))
    if review_skill := getattr(settings, "review_skill", "").strip():
        demands.append(SkillDemand("review_skill", review_skill))
        demands.extend(_field_demands("review_skill_alternates", getattr(settings, "review_skill_alternates", [])))
    fields = (
        "architectural_review_skill",
        "scanning_news_skill",
        "eval_local_skill",
        "backlog_sweep_skill",
        "dogfood_smoke_skill",
    )
    demands.extend(SkillDemand(field, skill) for field in fields if (skill := getattr(settings, field, "").strip()))
    return tuple(demands)


def skill_demand_names(demands: Iterable[SkillDemand]) -> tuple[str, ...]:
    return tuple(sorted({demand.name.rsplit(":", 1)[-1].strip() for demand in demands if demand.name.strip()}))
