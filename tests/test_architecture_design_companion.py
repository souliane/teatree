"""Tests for the architecture-design companion skill.

Verifies that:
- the skill file exists at the expected path with the expected frontmatter
- the nine architectural checks are enumerated in the ARCHITECTURE.md template
- the ``requires:`` graph wires it into ``code``, ``ticket``, and ``retro``
- transitive resolution loads ``architecture-design`` whenever any of the three implementer skills loads
"""

from pathlib import Path

from teatree.skill_support.deps import resolve_requires
from teatree.skill_support.schema import validate_skill_md
from teatree.trigger_parser import parse_triggers

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
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

    def test_declares_writing_plans_as_companion(self) -> None:
        text = SKILL_PATH.read_text(encoding="utf-8")
        triggers = parse_triggers(text)
        assert triggers is not None
        assert "writing-plans" in triggers["companions"]

    def test_template_enumerates_all_nine_sections(self) -> None:
        body = SKILL_PATH.read_text(encoding="utf-8")
        for section in EXPECTED_TEMPLATE_SECTIONS:
            assert section in body, f"ARCHITECTURE.md template missing section: {section}"

    def test_skill_under_two_hundred_lines(self) -> None:
        lines = SKILL_PATH.read_text(encoding="utf-8").splitlines()
        assert len(lines) <= 200, f"skill is {len(lines)} lines, cap is 200"


def _trigger_index_for(*skill_names: str) -> list[dict[str, object]]:
    """Build a real trigger index from the on-disk SKILL.md files."""
    entries: list[dict[str, object]] = []
    for name in skill_names:
        path = SKILLS_DIR / name / "SKILL.md"
        triggers = parse_triggers(path.read_text(encoding="utf-8"))
        if triggers is None:
            entries.append({"skill": name, "requires": [], "companions": []})
            continue
        entries.append({"skill": name, **triggers})
    return entries


class TestRequiresWiring:
    def test_code_requires_architecture_design(self) -> None:
        triggers = parse_triggers((SKILLS_DIR / "code" / "SKILL.md").read_text(encoding="utf-8"))
        assert triggers is not None
        assert "architecture-design" in triggers["requires"]

    def test_ticket_requires_architecture_design(self) -> None:
        triggers = parse_triggers((SKILLS_DIR / "ticket" / "SKILL.md").read_text(encoding="utf-8"))
        assert triggers is not None
        assert "architecture-design" in triggers["requires"]

    def test_retro_requires_architecture_design(self) -> None:
        triggers = parse_triggers((SKILLS_DIR / "retro" / "SKILL.md").read_text(encoding="utf-8"))
        assert triggers is not None
        assert "architecture-design" in triggers["requires"]


class TestResolution:
    def test_loading_code_pulls_architecture_design(self) -> None:
        index = _trigger_index_for("rules", "workspace", "architecture-design", "code")
        resolved = resolve_requires(["code"], index)
        assert "architecture-design" in resolved
        assert "code" in resolved
        # Dep order: dependencies come before dependents.
        assert resolved.index("architecture-design") < resolved.index("code")

    def test_loading_ticket_pulls_architecture_design(self) -> None:
        index = _trigger_index_for("rules", "workspace", "architecture-design", "ticket")
        resolved = resolve_requires(["ticket"], index)
        assert "architecture-design" in resolved
        assert resolved.index("architecture-design") < resolved.index("ticket")

    def test_loading_retro_pulls_architecture_design(self) -> None:
        index = _trigger_index_for("rules", "workspace", "architecture-design", "retro")
        resolved = resolve_requires(["retro"], index)
        assert "architecture-design" in resolved
        assert resolved.index("architecture-design") < resolved.index("retro")

    def test_no_circular_dependency(self) -> None:
        index = _trigger_index_for("rules", "workspace", "architecture-design", "code", "ticket", "retro")
        # If a cycle existed, resolve_requires would raise ValueError.
        resolve_requires(["code", "ticket", "retro"], index)

    def test_architecture_design_has_no_required_deps(self) -> None:
        triggers = parse_triggers(SKILL_PATH.read_text(encoding="utf-8"))
        assert triggers is not None
        # The companion stays at the bottom of the DAG — pulling it in must not
        # cascade further `requires:` loads.
        assert triggers["requires"] == []
