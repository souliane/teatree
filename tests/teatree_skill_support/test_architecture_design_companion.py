"""Tests for the architecture-design companion skill.

Verifies that:
- the skill file exists at the expected path with valid frontmatter
- the nine architectural checks are enumerated in the ARCHITECTURE.md template
- the ``requires:`` graph wires it into ``code``, ``ticket``, and ``retro``
- transitive resolution loads ``architecture-design`` whenever any of the three implementer skills loads
"""

from pathlib import Path

from teatree.skill_support.deps import resolve_requires
from teatree.skill_support.requires_parser import parse_requires
from teatree.skill_support.schema import validate_skill_md

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"
SKILL_PATH = SKILLS_DIR / "architecture-design" / "SKILL.md"

EXPECTED_TEMPLATE_SECTIONS = [
    "1. BLUEPRINT § alignment",
    "2. FSM phase boundaries",
    "3. Extension-point contracts",
    "4. Component boundaries",
    "5. Dependency direction",
    "6. Test surface",
    "7. Resilience invariants",
    "8. Identity and key normalization",
    "9. Behavior preservation / capability deletion",
]


def _requires_of(name: str) -> list[str]:
    return parse_requires((SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")) or []


class TestSkillFile:
    def test_skill_file_exists(self) -> None:
        assert SKILL_PATH.is_file(), f"architecture-design skill missing at {SKILL_PATH}"

    def test_frontmatter_is_valid(self) -> None:
        known = {p.name for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file()}
        errors, _warnings = validate_skill_md(SKILL_PATH, known_skills=known)
        assert errors == [], f"validation errors: {errors}"

    def test_frontmatter_name_and_description(self) -> None:
        text = SKILL_PATH.read_text(encoding="utf-8")
        assert "name: architecture-design" in text
        assert "description:" in text
        assert "architecture" in text.lower()

    def test_declares_writing_plans_as_required(self) -> None:
        # ``writing-plans`` (an external superpowers methodology skill) migrated
        # from the removed ``companions:`` key into the single ``requires:`` list.
        assert "writing-plans" in _requires_of("architecture-design")

    def test_template_enumerates_all_nine_sections(self) -> None:
        body = SKILL_PATH.read_text(encoding="utf-8")
        for section in EXPECTED_TEMPLATE_SECTIONS:
            assert section in body, f"ARCHITECTURE.md template missing section: {section}"

    def test_skill_under_two_hundred_lines(self) -> None:
        lines = SKILL_PATH.read_text(encoding="utf-8").splitlines()
        assert len(lines) <= 200, f"skill is {len(lines)} lines, cap is 200"


def _requires_index_for(*skill_names: str) -> list[dict[str, object]]:
    """Build a real requires index from the on-disk SKILL.md files."""
    return [{"skill": name, "requires": _requires_of(name)} for name in skill_names]


class TestRequiresWiring:
    def test_code_requires_architecture_design(self) -> None:
        assert "architecture-design" in _requires_of("code")

    def test_ticket_requires_architecture_design(self) -> None:
        assert "architecture-design" in _requires_of("ticket")

    def test_retro_requires_architecture_design(self) -> None:
        assert "architecture-design" in _requires_of("retro")


class TestResolution:
    def test_loading_code_pulls_architecture_design(self) -> None:
        index = _requires_index_for("rules", "workspace", "architecture-design", "code")
        resolved = resolve_requires(["code"], index)
        assert "architecture-design" in resolved
        assert "code" in resolved
        # Dep order: dependencies come before dependents.
        assert resolved.index("architecture-design") < resolved.index("code")

    def test_loading_ticket_pulls_architecture_design(self) -> None:
        index = _requires_index_for("rules", "workspace", "architecture-design", "ticket")
        resolved = resolve_requires(["ticket"], index)
        assert "architecture-design" in resolved
        assert resolved.index("architecture-design") < resolved.index("ticket")

    def test_loading_retro_pulls_architecture_design(self) -> None:
        index = _requires_index_for("rules", "workspace", "architecture-design", "retro")
        resolved = resolve_requires(["retro"], index)
        assert "architecture-design" in resolved
        assert resolved.index("architecture-design") < resolved.index("retro")

    def test_no_circular_dependency(self) -> None:
        index = _requires_index_for("rules", "workspace", "architecture-design", "code", "ticket", "retro")
        # If a cycle existed, resolve_requires would raise ValueError.
        resolve_requires(["code", "ticket", "retro"], index)

    def test_architecture_design_requires_writing_plans(self) -> None:
        # writing-plans is an external methodology skill with no SKILL.md in this
        # repo; it stays in ``requires`` and passes through resolution.
        assert _requires_of("architecture-design") == ["writing-plans"]
