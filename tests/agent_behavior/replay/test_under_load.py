"""Under-load harness prompt construction — skill bundle + polluted preamble.

Pins the two harness extensions: the ``under_load`` lane loads the FULL skill
bundle into the system prompt, and ``context_preamble`` is folded into the user
prompt text (the SDK user-turns-only constraint). The ``clean_room`` lane must
stay byte-identical to today.
"""
# test-path: cross-cutting — an eval-lane test living under tests/agent_behavior/ by
# the established eval-suite convention.

from pathlib import Path

from teatree.eval.models import EvalSpec
from teatree.eval.prompt_framing import SKILL_BUNDLE_FRAMING
from teatree.eval.under_load import build_system_prompt, build_user_prompt, load_skill_bundle


def _spec(*, lane: str, prompt: str = "do the thing", context_preamble: str = "") -> EvalSpec:
    return EvalSpec(
        name="synthetic",
        scenario="synthetic",
        agent_path="skills/rules/SKILL.md",
        prompt=prompt,
        matchers=(),
        source_path=Path("synthetic.yaml"),
        lane=lane,
        context_preamble=context_preamble,
    )


def _bundle_skill_dir(tmp_path: Path) -> Path:
    skills = tmp_path / "skills"
    for name, body in (("alpha", "# Alpha\n\nrule one"), ("beta", "# Beta\n\nrule two")):
        skill_dir = skills / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    return skills


class TestLoadSkillBundle:
    def test_concatenates_every_skill_with_a_named_header(self, tmp_path: Path) -> None:
        bundle = load_skill_bundle(skills_dir=_bundle_skill_dir(tmp_path))
        assert "## skill: alpha" in bundle
        assert "## skill: beta" in bundle
        assert "rule one" in bundle
        assert "rule two" in bundle

    def test_skips_skill_dir_with_no_skill_md(self, tmp_path: Path) -> None:
        skills = _bundle_skill_dir(tmp_path)
        (skills / "empty").mkdir()
        bundle = load_skill_bundle(skills_dir=skills)
        assert "## skill: empty" not in bundle


class TestBuildSystemPrompt:
    def test_clean_room_returns_the_single_skill_prompt_byte_identical(self, tmp_path: Path) -> None:
        clean = "SINGLE SKILL BODY + framing"
        result = build_system_prompt(
            _spec(lane="clean_room"), clean_room_prompt=clean, skills_dir=_bundle_skill_dir(tmp_path)
        )
        assert result == clean

    def test_under_load_loads_the_full_bundle_not_the_single_skill(self, tmp_path: Path) -> None:
        clean = "SINGLE SKILL BODY"
        result = build_system_prompt(
            _spec(lane="under_load"), clean_room_prompt=clean, skills_dir=_bundle_skill_dir(tmp_path)
        )
        assert result != clean
        assert SKILL_BUNDLE_FRAMING in result
        assert "## skill: alpha" in result
        assert "## skill: beta" in result


class TestBuildUserPrompt:
    def test_no_preamble_returns_the_prompt_unchanged(self) -> None:
        spec = _spec(lane="clean_room", prompt="just the task")
        assert build_user_prompt(spec) == "just the task"

    def test_preamble_is_folded_in_before_the_prompt(self) -> None:
        spec = _spec(lane="under_load", prompt="THE TASK", context_preamble="POLLUTED PREFIX")
        result = build_user_prompt(spec)
        assert result.startswith("POLLUTED PREFIX")
        assert result.endswith("THE TASK")
        assert "THE TASK" in result
