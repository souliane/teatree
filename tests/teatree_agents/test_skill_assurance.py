"""A requested headless skill must be delivered, then honestly accounted for."""

from pathlib import Path

import pytest

from teatree.agents.attempt_recorder import AttemptUsage, with_transport_records
from teatree.agents.coding_prompt import _coding_phase_directive
from teatree.agents.prompt import required_skill_delivery
from teatree.agents.result_schema import RESULT_JSON_SCHEMA
from teatree.agents.skill_assurance import (
    SkillDispatchError,
    assess_skill_application,
    assess_skill_dispatch,
    recover_truncated_inline_skills,
)
from teatree.agents.skill_injection import _read_skill_contents_scoped


def _skill(root: Path, name: str) -> None:
    directory = root / name
    directory.mkdir()
    (directory / "SKILL.md").write_text(f"# {name}\nApply this skill.\n", encoding="utf-8")


def test_missing_mandatory_skill_is_a_pre_run_refusal(tmp_path: Path) -> None:
    with pytest.raises(SkillDispatchError) as caught:
        assess_skill_dispatch(
            skills=["t3:code"],
            required_inline={"t3:code"},
            required_explicit=set(),
            rendered_context="",
            skills_dirs=[tmp_path],
        )

    assert caught.value.assurance["status"] == "missing"
    assert caught.value.assurance["requested"] == ["code"]
    assert caught.value.assurance["missing"] == ["code"]


def test_a_found_skill_with_missing_injected_body_is_a_pre_run_refusal(tmp_path: Path) -> None:
    _skill(tmp_path, "code")

    with pytest.raises(SkillDispatchError) as caught:
        assess_skill_dispatch(
            skills=["t3:code"],
            required_inline={"t3:code"},
            required_explicit=set(),
            rendered_context="# Loaded Skills\n(no code body)",
            skills_dirs=[tmp_path],
        )

    assert caught.value.assurance["status"] == "injection_gap"
    assert caught.value.assurance["found"] == ["code"]
    assert caught.value.assurance["injected"] == []


def test_explicit_load_is_valid_without_inlining_and_optional_companion_is_not_mandatory(tmp_path: Path) -> None:
    _skill(tmp_path, "backend-dev")

    assurance = assess_skill_dispatch(
        skills=["backend-dev", "optional-companion"],
        required_inline=set(),
        required_explicit={"backend-dev"},
        rendered_context="Load /backend-dev via the Skill tool BEFORE reviewing.",
        skills_dirs=[tmp_path],
    )

    assert assurance["requested"] == ["backend-dev"]
    assert assurance["found"] == ["backend-dev"]
    assert assurance["explicit_load"] == ["backend-dev"]
    assert assurance["missing"] == []


def test_unrelated_or_missing_application_receipt_never_proves_use(tmp_path: Path) -> None:
    _skill(tmp_path, "code")
    dispatch = assess_skill_dispatch(
        skills=["t3:code"],
        required_inline={"t3:code"},
        required_explicit=set(),
        rendered_context="--- SKILL: code ---\n# code\nApply this skill.\n",
        skills_dirs=[tmp_path],
    )

    absent = assess_skill_application(dispatch, {})
    unrelated = assess_skill_application(
        dispatch,
        {"skill_application": [{"skill": "other", "evidence": "claimed"}]},
    )
    declared = assess_skill_application(
        dispatch,
        {"skill_application": [{"skill": "t3:code", "evidence": "tests/test_change.py::test_case passed"}]},
    )

    assert absent["status"] == "unverified"
    assert unrelated["status"] == "unverified"
    assert unrelated["evidence"] == []
    assert declared["status"] == "declared"
    assert declared["evidence"] == [{"skill": "code", "evidence": "provided"}]


def test_empty_requested_set_never_counts_as_declared() -> None:
    dispatch = assess_skill_dispatch(
        skills=[], required_inline=set(), required_explicit=set(), rendered_context="", skills_dirs=[]
    )

    assert assess_skill_application(dispatch, {})["status"] == "unverified"


def test_every_attempt_recorder_path_scrubs_raw_application_references() -> None:
    result = {
        "summary": "finished before interruption",
        "skill_application": [{"skill": "code", "evidence": "private/secret-path"}],
    }

    recorded = with_transport_records(result, AttemptUsage())

    assert recorded == {"summary": "finished before interruption"}
    assert "secret-path" not in str(recorded)


def test_reactive_phase_requires_every_all_inline_skill() -> None:
    inline, explicit = required_skill_delivery(
        "answering", ["t3:answer", "rules", "overlay-companion"], lifecycle_skill="", stage_skills=[]
    )

    assert inline == {"t3:answer", "rules", "overlay-companion"}
    assert explicit == set()


def test_explicit_skill_requires_observed_load_even_when_agent_claims_use(tmp_path: Path) -> None:
    _skill(tmp_path, "code")
    dispatch = assess_skill_dispatch(
        skills=["t3:code"],
        required_inline=set(),
        required_explicit={"code"},
        rendered_context="Load /code via the Skill tool before work.",
        skills_dirs=[tmp_path],
    )
    claimed = {"skill_application": [{"skill": "code", "evidence": "tests/test_case.py passed"}]}

    assert assess_skill_application(dispatch, claimed)["status"] == "unverified"
    observed = assess_skill_application(dispatch, claimed, observed_loads=["t3:code"])
    assert observed["status"] == "declared"
    assert observed["observed_loads"] == ["code"]


def test_coding_delivery_requires_scoped_skills_but_not_optional_companions() -> None:
    inline, explicit = required_skill_delivery(
        "coding",
        ["rules", "code", "architecture-design", "ac-python", "optional-companion"],
        lifecycle_skill="code",
        stage_skills=[],
    )

    assert {"rules", "code", "architecture-design"} <= inline
    assert {"code", "architecture-design", "ac-python", "optional-companion"} <= explicit


def test_namespaced_primary_skill_is_embedded_in_full(tmp_path: Path) -> None:
    _skill(tmp_path, "code")

    text = _read_skill_contents_scoped(["t3:code"], primary_skills={"code"}, skills_dir=tmp_path)

    assert "--- SKILL: code ---" in text


def test_truncated_inline_skill_requires_explicit_load_of_the_full_file(tmp_path: Path) -> None:
    _skill(tmp_path, "code")

    inline, explicit, directive = recover_truncated_inline_skills(
        required_inline={"code"},
        required_explicit=set(),
        rendered_context="--- SKILL: code ---\n# code\n[…truncated]",
        skills_dirs=[tmp_path],
        can_load=True,
    )

    assert inline == set()
    assert explicit == {"code"}
    assert "Load /code" in directive
    assert str(tmp_path / "code" / "SKILL.md") in directive
    assurance = assess_skill_dispatch(
        skills=["t3:code"],
        required_inline=inline,
        required_explicit=explicit,
        rendered_context=directive,
        skills_dirs=[tmp_path],
    )
    assert assurance["injected"] == []
    assert assurance["explicit_load"] == ["code"]


def test_explicit_directive_must_be_exact_skill_reference(tmp_path: Path) -> None:
    _skill(tmp_path, "backend-dev")

    with pytest.raises(SkillDispatchError, match="injection_gap"):
        assess_skill_dispatch(
            skills=["backend-dev"],
            required_inline=set(),
            required_explicit={"backend-dev"},
            rendered_context="A file at /backend-dev/README mentions the skill.",
            skills_dirs=[tmp_path],
        )


def test_other_skill_mention_does_not_replace_required_load_directive(tmp_path: Path) -> None:
    _skill(tmp_path, "backend-dev")

    with pytest.raises(SkillDispatchError, match="injection_gap"):
        assess_skill_dispatch(
            skills=["backend-dev"],
            required_inline=set(),
            required_explicit={"backend-dev"},
            rendered_context="--- SKILL: rules ---\n# Rules\nSee /backend-dev before editing Python.\n",
            skills_dirs=[tmp_path],
        )


def test_coding_prompt_delivers_architecture_code_and_stack_loads(tmp_path: Path) -> None:
    names = ["architecture-design", "code", "ac-django", "demo-overlay"]
    for name in names:
        _skill(tmp_path, name)
    rendered = "\n".join(_coding_phase_directive(["ac-django", "t3:demo-overlay", "code"]))

    assurance = assess_skill_dispatch(
        skills=names,
        required_inline=set(),
        required_explicit=set(names),
        rendered_context=rendered,
        skills_dirs=[tmp_path],
    )

    assert assurance["explicit_load"] == names


def test_skill_application_is_an_allowed_envelope_key_and_dispatch_receipt_is_out_of_band() -> None:
    properties = RESULT_JSON_SCHEMA["properties"]
    assert "skill_application" in properties
    assurance = {
        "requested": ["code"],
        "found": ["code"],
        "injected": ["code"],
        "explicit_load": [],
        "missing": [],
        "evidence": [],
        "observed_loads": [],
        "status": "unverified",
    }
    result = with_transport_records({"summary": "done"}, AttemptUsage(skill_assurance=assurance))

    assert result["skill_assurance"] == assurance
    assert "skill_application" not in result
