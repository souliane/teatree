"""Under-load harness prompt construction — skill bundle + polluted preamble.

Pins the two harness extensions: the ``under_load`` lane loads the FULL skill
bundle into the system prompt, and ``context_preamble`` is folded into the user
prompt text (the SDK user-turns-only constraint). The ``clean_room`` lane must
stay byte-identical to today.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

from pathlib import Path

from teatree.eval.models import EvalSpec
from teatree.eval.prompt_framing import SKILL_BUNDLE_FRAMING
from teatree.eval.under_load import (
    SKILLS_DIR,
    build_system_prompt,
    build_user_prompt,
    load_budgeted_skill_bundle,
    load_skill_bundle,
)


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


class TestLoadBudgetedSkillBundle:
    def _big_skill_dir(self, tmp_path: Path) -> Path:
        # Six skills, four of them large, so the budget forces a trim.
        skills = tmp_path / "skills"
        bodies = {
            "rules": "# Rules\n\n" + ("rule " * 200),
            "wip": "# Wip\n\n" + ("wip " * 50),
            "loops": "# Loops\n\nthe role split source",  # tiny canonical-source skill
            "ship": "# Ship\n\n" + ("ship " * 4000),
            "review": "# Review\n\n" + ("review " * 4000),
            "e2e": "# E2E\n\n" + ("e2e " * 4000),
        }
        for name, body in bodies.items():
            (skills / name).mkdir(parents=True)
            (skills / name / "SKILL.md").write_text(body, encoding="utf-8")
        return skills

    def test_small_catalog_under_budget_keeps_every_skill(self, tmp_path: Path) -> None:
        skills = _bundle_skill_dir(tmp_path)
        budgeted = load_budgeted_skill_bundle(char_budget=1_000_000, skills_dir=skills)
        assert budgeted == load_skill_bundle(skills_dir=skills)

    def test_over_budget_keeps_agent_path_skill_rules_and_loops(self, tmp_path: Path) -> None:
        skills = self._big_skill_dir(tmp_path)
        budgeted = load_budgeted_skill_bundle(keep_skill="wip", char_budget=15_000, skills_dir=skills)
        assert "## skill: wip" in budgeted, "the agent_path skill (keep_skill) must never be dropped"
        assert "## skill: rules" in budgeted, "the always-keep cross-cutting rules skill must survive"
        assert "## skill: loops" in budgeted, "a small canonical-source skill must survive smallest-first"

    def test_over_budget_sheds_the_largest_tail(self, tmp_path: Path) -> None:
        skills = self._big_skill_dir(tmp_path)
        budgeted = load_budgeted_skill_bundle(keep_skill="wip", char_budget=15_000, skills_dir=skills)
        # Only one of the three large peripheral skills can fit beside the pinned set.
        large_present = sum(f"## skill: {n}" in budgeted for n in ("ship", "review", "e2e"))
        assert large_present < 3, "the budget did not shed any large tail skill"

    def test_budgeted_bundle_never_exceeds_the_char_budget(self, tmp_path: Path) -> None:
        skills = self._big_skill_dir(tmp_path)
        budgeted = load_budgeted_skill_bundle(keep_skill="wip", char_budget=15_000, skills_dir=skills)
        # The pinned set (wip+rules+loops) is small here; the cap holds for the fill.
        assert len(budgeted) <= 15_000

    def test_real_catalog_under_load_prompt_fits_the_input_window(self) -> None:
        # The end-to-end guard: every shipped skill in the real catalog, budgeted,
        # plus framing, must leave ample room under the 200k-token (~800k-char)
        # window for the preamble, tools, and response. ~600k chars (~150k tokens).
        bundle = load_budgeted_skill_bundle(keep_skill="rules", skills_dir=SKILLS_DIR)
        framed = SKILL_BUNDLE_FRAMING + bundle
        assert len(framed) < 640_000, (
            f"budgeted under_load system prompt is {len(framed):,} chars (~{len(framed) // 4:,} tok) — "
            "too large to leave room for the preamble + tool schemas + response"
        )
        assert "## skill: loops" in bundle, "the role-split canonical source must stay in the real bundle"
        assert "## skill: rules" in bundle, "the cross-cutting rules skill must stay in the real bundle"


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
