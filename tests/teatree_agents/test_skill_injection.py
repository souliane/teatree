"""Tests for teatree.agents.skill_injection — rendering loaded skills into prompt text."""

from pathlib import Path

from teatree.agents.skill_injection import (
    _is_primary,
    _read_skill_contents,
    _read_skill_contents_scoped,
    build_subagent_skill_preamble,
)

# --- _read_skill_contents ---


def test_read_skill_contents_reads_existing_skill(tmp_path: Path) -> None:
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# My Skill\nDo stuff.", encoding="utf-8")

    result = _read_skill_contents(["my-skill"], skills_dir=tmp_path)
    assert "--- SKILL: my-skill ---" in result
    assert "# My Skill" in result


def test_read_skill_contents_skips_missing_skill(tmp_path: Path) -> None:
    result = _read_skill_contents(["nonexistent"], skills_dir=tmp_path)
    assert result == ""


def test_read_skill_contents_multiple_skills(tmp_path: Path) -> None:
    for name in ("skill-a", "skill-b"):
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"# {name}", encoding="utf-8")

    result = _read_skill_contents(["skill-a", "skill-b"], skills_dir=tmp_path)
    assert "--- SKILL: skill-a ---" in result
    assert "--- SKILL: skill-b ---" in result


# --- _is_primary ---


def test_is_primary_matches_short_name() -> None:
    assert _is_primary("test", {"test"})
    assert not _is_primary("code", {"test"})


def test_is_primary_matches_always_full() -> None:
    assert _is_primary("rules", set())


def test_is_primary_matches_absolute_path() -> None:
    assert _is_primary("/tmp/skills/test/SKILL.md", {"test"})
    assert _is_primary("/tmp/skills/rules/SKILL.md", set())
    assert not _is_primary("/tmp/skills/ac-django/SKILL.md", {"test"})


# --- _read_skill_contents_scoped ---


def test_read_scoped_embeds_primary_and_summarizes_companions(tmp_path: Path) -> None:
    for name in ("rules", "test", "ac-django", "workspace"):
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"# {name} full content", encoding="utf-8")

    result = _read_skill_contents_scoped(
        ["ac-django", "workspace", "rules", "test"],
        primary_skills={"test"},
        skills_dir=tmp_path,
    )
    # Primary skills get full content
    assert "--- SKILL: test ---" in result
    assert "# test full content" in result
    # rules is always fully loaded
    assert "--- SKILL: rules ---" in result
    assert "# rules full content" in result
    # Companion skills get summary only
    assert "COMPANION SKILLS" in result
    assert "- ac-django:" in result
    assert "- workspace:" in result
    assert "# ac-django full content" not in result
    assert "# workspace full content" not in result


def test_read_scoped_all_primary(tmp_path: Path) -> None:
    d = tmp_path / "only-skill"
    d.mkdir()
    (d / "SKILL.md").write_text("# Only", encoding="utf-8")

    result = _read_skill_contents_scoped(
        ["only-skill"],
        primary_skills={"only-skill"},
        skills_dir=tmp_path,
    )
    assert "--- SKILL: only-skill ---" in result
    assert "COMPANION" not in result


def test_read_scoped_missing_skill(tmp_path: Path) -> None:
    result = _read_skill_contents_scoped(
        ["nonexistent"],
        primary_skills={"nonexistent"},
        skills_dir=tmp_path,
    )
    assert result == ""


# --- build_subagent_skill_preamble ---


def _write_skill(skills_dir: Path, name: str, body: str) -> None:
    d = skills_dir / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


def test_subagent_preamble_embeds_framework_skill_body(tmp_path: Path) -> None:
    framework = tmp_path / "framework"
    _write_skill(framework, "rules", "# Rules\nNever edit the main clone.")

    preamble = build_subagent_skill_preamble(["rules"], skills_dirs=[framework])

    assert "--- SKILL: rules ---" in preamble.text
    assert "Never edit the main clone." in preamble.text
    assert "does not auto-load" in preamble.text
    assert preamble.resolved == ["rules"]
    assert preamble.missing == []


def test_subagent_preamble_resolves_overlay_skill_from_second_dir(tmp_path: Path) -> None:
    framework = tmp_path / "framework"
    overlay = tmp_path / "overlay"
    _write_skill(framework, "rules", "# Rules body")
    _write_skill(overlay, "acme", "# Acme overlay\nUse the t3 acme CLI, never raw glab.")

    preamble = build_subagent_skill_preamble(["rules", "acme"], skills_dirs=[framework, overlay])

    assert "Use the t3 acme CLI, never raw glab." in preamble.text
    assert preamble.resolved == ["rules", "acme"]
    assert preamble.missing == []


def test_subagent_preamble_framework_wins_over_overlay_on_name_collision(tmp_path: Path) -> None:
    framework = tmp_path / "framework"
    overlay = tmp_path / "overlay"
    _write_skill(framework, "rules", "# FRAMEWORK rules body")
    _write_skill(overlay, "rules", "# OVERLAY rules body")

    preamble = build_subagent_skill_preamble(["rules"], skills_dirs=[framework, overlay])

    assert "# FRAMEWORK rules body" in preamble.text
    assert "# OVERLAY rules body" not in preamble.text


def test_subagent_preamble_strips_namespace_qualifier(tmp_path: Path) -> None:
    framework = tmp_path / "framework"
    _write_skill(framework, "e2e", "# E2E\nReuse the dev-env, do not over-provision.")

    preamble = build_subagent_skill_preamble(["t3:e2e"], skills_dirs=[framework])

    assert "--- SKILL: e2e ---" in preamble.text
    assert "do not over-provision." in preamble.text
    assert preamble.resolved == ["e2e"]


def test_subagent_preamble_reports_missing_without_silently_dropping(tmp_path: Path) -> None:
    framework = tmp_path / "framework"
    _write_skill(framework, "rules", "# Rules body")

    preamble = build_subagent_skill_preamble(["rules", "nope"], skills_dirs=[framework])

    assert preamble.resolved == ["rules"]
    assert preamble.missing == ["nope"]
    assert "nope" not in preamble.text


def test_subagent_preamble_preserves_order(tmp_path: Path) -> None:
    framework = tmp_path / "framework"
    for name in ("rules", "e2e", "code"):
        _write_skill(framework, name, f"# {name} body")

    preamble = build_subagent_skill_preamble(["e2e", "rules", "code"], skills_dirs=[framework])

    assert preamble.text.index("SKILL: e2e") < preamble.text.index("SKILL: rules") < preamble.text.index("SKILL: code")


def test_subagent_preamble_empty_when_nothing_resolves(tmp_path: Path) -> None:
    preamble = build_subagent_skill_preamble(["ghost"], skills_dirs=[tmp_path])

    assert preamble.text == ""
    assert preamble.resolved == []
    assert preamble.missing == ["ghost"]


def test_subagent_preamble_resolves_explicit_skill_md_path_form(tmp_path: Path) -> None:
    framework = tmp_path / "framework"
    _write_skill(framework, "rules", "# Rules body")

    preamble = build_subagent_skill_preamble(["skills/rules/SKILL.md"], skills_dirs=[framework])

    assert "--- SKILL: rules ---" in preamble.text
    assert preamble.resolved == ["rules"]
