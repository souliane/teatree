import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

from teatree.config.cold_defaults import flatten_settings_table, shipped_defaults_table
from teatree.config.settings import UserSettings
from teatree.core.overlay import OverlayConfig
from teatree.loop.scanners.eval_local import EvalLocalScanner
from teatree.loop.scanners.provision_smoke import ProvisionSmokeScanner
from teatree.skill_support.demands import LOOP_SKILL_FIELDS, SkillDemand, enumerate_skill_demands, skill_demand_names

SHIPPED_SKILLS = Path(__file__).resolve().parents[2] / "skills"


def test_every_dispatching_overlay_and_runtime_field_is_enumerated() -> None:
    config = SimpleNamespace(
        stage_skills={"planning": ["spec-writer"], "testing": ["qa"]},
        companion_skills=["domain-primer"],
        pr_review_companion="code-review",
    )
    settings = SimpleNamespace(
        review_skill="backend-review",
        review_skill_alternates=["codex-review"],
        architectural_review_skill="ac-reviewing-codebase",
        scanning_news_skill="scanning-news",
        eval_local_skill="running-evals",
        backlog_sweep_skill="sweeping-tickets",
        dogfood_smoke_skill="dogfooding",
    )

    assert enumerate_skill_demands(config, settings) == (
        SkillDemand("stage_skills[planning]", "spec-writer"),
        SkillDemand("stage_skills[testing]", "qa"),
        SkillDemand("companion_skills", "domain-primer"),
        SkillDemand("pr_review_companion", "code-review"),
        SkillDemand("review_skill", "backend-review"),
        SkillDemand("review_skill_alternates", "codex-review"),
        SkillDemand("architectural_review_skill", "ac-reviewing-codebase"),
        SkillDemand("scanning_news_skill", "scanning-news"),
        SkillDemand("eval_local_skill", "running-evals"),
        SkillDemand("backlog_sweep_skill", "sweeping-tickets"),
        SkillDemand("dogfood_smoke_skill", "dogfooding"),
    )


def test_review_alternates_do_not_dispatch_without_a_primary_review_skill() -> None:
    config = SimpleNamespace(stage_skills={}, companion_skills=[], pr_review_companion="")
    settings = SimpleNamespace(review_skill="", review_skill_alternates=["codex-review"])

    assert enumerate_skill_demands(config, settings) == ()


def test_demand_names_are_normalized_deduped_and_sorted() -> None:
    demands = (
        SkillDemand("first", "t3:qa"),
        SkillDemand("second", " qa "),
        SkillDemand("third", "backend-review"),
        SkillDemand("empty", " "),
    )

    assert skill_demand_names(demands) == ("backend-review", "qa")


@pytest.mark.parametrize("field", LOOP_SKILL_FIELDS)
def test_every_loop_skill_default_names_a_shipped_skill(field: str) -> None:
    # Setup refuses a headless box whose demanded skills are not loadable, so a
    # default naming no shipped skill blocks every deploy.
    defaults = {
        "UserSettings": getattr(UserSettings(), field),
        "OverlayConfig": OverlayConfig.model_fields[field].default,
        "defaults.toml": flatten_settings_table(shipped_defaults_table())[field],
    }

    missing = {source: name for source, name in defaults.items() if not (SHIPPED_SKILLS / name / "SKILL.md").is_file()}

    assert not missing, f"{field} defaults name no shipped skill: {missing}"


@pytest.mark.parametrize("scanner", [EvalLocalScanner, ProvisionSmokeScanner])
def test_loop_scanners_take_their_skill_from_the_setting_not_a_literal_of_their_own(scanner: type) -> None:
    skill = next(f for f in dataclasses.fields(scanner) if f.name == "skill")

    assert skill.default is dataclasses.MISSING
