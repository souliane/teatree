from types import SimpleNamespace

from teatree.skill_support.demands import SkillDemand, enumerate_skill_demands, skill_demand_names


def test_every_dispatching_overlay_and_runtime_field_is_enumerated() -> None:
    config = SimpleNamespace(
        stage_skills={"planning": ["prd-agent"], "testing": ["qa"]},
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
        SkillDemand("stage_skills[planning]", "prd-agent"),
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
