from types import SimpleNamespace

import pytest

from teatree.core import skill_sources
from teatree.harness_skills import SkillsHarness
from teatree.provisioning.skill_clone_install import CloneInstall
from teatree.provisioning.skill_drift import SkillSourceClone
from teatree.skill_support.demands import SkillDemand


def test_overlay_demands_use_each_overlays_effective_runtime_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    overlay = SimpleNamespace(
        config=SimpleNamespace(
            stage_skills={"coding": ["backend-dev"]},
            companion_skills=[],
            pr_review_companion="",
        )
    )
    seen: list[str] = []
    monkeypatch.setattr(skill_sources, "get_all_overlays", lambda: {"t3-acme": overlay})

    def settings_for(overlay_name: str) -> SimpleNamespace:
        seen.append(overlay_name)
        return SimpleNamespace(review_skill="backend-review", review_skill_alternates=[])

    monkeypatch.setattr("teatree.config.get_effective_settings", settings_for)

    assert skill_sources.skill_demands_by_overlay() == {
        "t3-acme": (
            SkillDemand("stage_skills[coding]", "backend-dev"),
            SkillDemand("review_skill", "backend-review"),
        )
    }
    assert seen == ["t3-acme"]
    assert {"backend-dev", "backend-review"}.issubset(skill_sources.demanded_skill_names())


def test_dispatchable_agent_declarations_are_mandatory_at_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skill_sources, "skill_demands_by_overlay", dict)
    monkeypatch.setattr(skill_sources, "SUBAGENT_BY_PHASE", {("author", "coding"): "t3:coder"})
    monkeypatch.setattr(
        skill_sources, "declared_skills_for_agent", lambda agent: ["agent-only"] if agent == "coder" else []
    )

    assert "agent-only" in skill_sources.demanded_skill_names()


def test_dispatch_phase_without_declaration_requires_runtime_lifecycle_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(skill_sources, "skill_demands_by_overlay", dict)
    monkeypatch.setattr(skill_sources, "SUBAGENT_BY_PHASE", {("author", "coding"): "t3:coder"})
    monkeypatch.setattr(skill_sources, "declared_skills_for_agent", lambda _agent: [])

    assert "code" in skill_sources.demanded_skill_names()


def test_overlay_without_config_has_no_demands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skill_sources, "get_all_overlays", lambda: {"broken": SimpleNamespace()})

    assert skill_sources.skill_demands_by_overlay() == {}


def test_declared_source_install_receives_demands_exclusions_and_cli(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clone = SkillSourceClone(label="team/skills")
    monkeypatch.setattr(skill_sources, "declared_skill_sources", lambda: [clone])
    seen: list[object] = []

    def install(source: SkillSourceClone, **kwargs: object) -> CloneInstall:
        seen.append((source, kwargs))
        return CloneInstall(label=source.label)

    monkeypatch.setattr(skill_sources, "install_published_skills", install)
    cli = object()

    result = skill_sources.install_declared_sources(
        cache_root=tmp_path,
        demand_names={"backend-dev"},
        harness_exclusions=["codex:backend-dev"],
        cli=cli,
    )

    assert result == [CloneInstall(label="team/skills")]
    assert seen == [
        (
            clone,
            {
                "cache_root": tmp_path,
                "demand_names": {"backend-dev"},
                "harness_exclusions": [f"{SkillsHarness.CODEX.value}:backend-dev"],
                "cli": cli,
            },
        )
    ]
