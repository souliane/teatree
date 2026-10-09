"""Eval scenarios live in one home — never inline in the skills/ tree.

The eval restructure (#2452) makes ``evals/scenarios/*.yaml`` the single
canonical home for cross-overlay scenario definitions (overlays add their own
via ``get_eval_scenarios_dir()``). A scenario body must NOT live co-located
beside a ``SKILL.md`` as ``skills/<name>/evals.yaml``: a skill directory carries
prose only, so a reader of the tree finds every eval under ``evals/`` and the
catalog has no split brain. Coverage is still attributed per skill through each
scenario's ``agent_path: skills/<name>/SKILL.md`` (see
``teatree.eval.coverage``), so centralising loses no per-skill gate.

This is a structural guard: a re-introduced ``skills/<name>/evals.yaml`` turns
it RED. The anti-vacuity proof (:func:`test_guard_flags_a_synthetic_inline_eval`)
constructs a synthetic skills tree carrying an ``evals.yaml`` and asserts the
predicate flags it, so reverting the guard to a no-op turns the proof RED too.
"""

from pathlib import Path

_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


def _inline_skill_evals(skills_dir: Path) -> list[str]:
    return sorted(str(p.relative_to(skills_dir.parent)) for p in skills_dir.glob("*/evals.yaml"))


def test_no_skill_ships_an_inline_evals_yaml() -> None:
    offenders = _inline_skill_evals(_SKILLS_DIR)
    assert offenders == [], (
        "skill(s) ship an inline evals.yaml — eval scenarios live only under "
        "evals/scenarios/ (with an explicit agent_path: skills/<name>/SKILL.md). "
        "Move the scenario body there and delete the co-located file:\n  " + "\n  ".join(offenders)
    )


def test_guard_flags_a_synthetic_inline_eval(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    (skills / "ship").mkdir(parents=True)
    (skills / "ship" / "SKILL.md").write_text("---\nname: ship\n---\n", encoding="utf-8")
    assert _inline_skill_evals(skills) == []

    (skills / "ship" / "evals.yaml").write_text("- name: x\n", encoding="utf-8")
    assert _inline_skill_evals(skills) == ["skills/ship/evals.yaml"]
