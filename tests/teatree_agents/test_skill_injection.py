"""Tests for teatree.agents.skill_injection — rendering loaded skills into prompt text."""

from pathlib import Path

import pytest

from teatree.agents import skill_injection
from teatree.agents.skill_injection import (
    _is_primary,
    _read_skill_contents,
    _read_skill_contents_scoped,
    _resolve_skill_md,
    build_subagent_skill_preamble,
    harness_skills_dirs,
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


# --- embed path agrees with the warn/resolve path (#3206) ---
# The unresolvable-warning check resolves via ``_resolve_skill_md`` (bare-name
# strip); the embed must resolve the same reference forms, or a namespaced /
# path-form stage skill warns as "resolvable" yet is silently dropped from the
# embed (or vice versa). Every case below asserts warn-resolution and embed agree.


def test_read_skill_contents_embeds_namespaced_name_like_the_resolver(tmp_path: Path) -> None:
    _write_skill(tmp_path, "backend-dev", "# backend-dev body")

    assert _resolve_skill_md("t3:backend-dev", [tmp_path]) is not None
    result = _read_skill_contents(["t3:backend-dev"], skills_dir=tmp_path)
    assert "--- SKILL: backend-dev ---" in result
    assert "# backend-dev body" in result


def test_read_skill_contents_embeds_path_form_name_like_the_resolver(tmp_path: Path) -> None:
    _write_skill(tmp_path, "rules", "# rules body")

    assert _resolve_skill_md("skills/rules/SKILL.md", [tmp_path]) is not None
    result = _read_skill_contents(["skills/rules/SKILL.md"], skills_dir=tmp_path)
    assert "--- SKILL: rules ---" in result
    assert "# rules body" in result


def test_read_scoped_embeds_namespaced_primary_like_the_resolver(tmp_path: Path) -> None:
    _write_skill(tmp_path, "backend-dev", "# backend-dev body")

    assert _resolve_skill_md("t3:backend-dev", [tmp_path]) is not None
    result = _read_skill_contents_scoped(
        ["t3:backend-dev"],
        primary_skills={"t3:backend-dev"},
        skills_dir=tmp_path,
    )
    assert "--- SKILL: backend-dev ---" in result
    assert "# backend-dev body" in result


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


def test_companion_line_names_the_absolute_skill_md_to_read(tmp_path: Path) -> None:
    for name in ("rules", "workspace"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "SKILL.md").write_text(f"# {name}", encoding="utf-8")

    result = _read_skill_contents_scoped(["rules", "workspace", "ghost"], primary_skills=set(), skills_dir=tmp_path)

    assert f"- workspace: not embedded — Read `{tmp_path / 'workspace' / 'SKILL.md'}` when it applies" in result
    assert "- ghost: not embedded — no SKILL.md resolves for it on this host" in result


def test_reach_line_leads_the_skill_block_before_the_first_embed(tmp_path: Path) -> None:
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "SKILL.md").write_text("# rules", encoding="utf-8")

    for result in (
        _read_skill_contents(["rules"], skills_dir=tmp_path),
        _read_skill_contents_scoped(["rules"], primary_skills=set(), skills_dir=tmp_path),
        build_subagent_skill_preamble(["rules"], skills_dirs=[tmp_path]).text,
    ):
        reach = result.index(f"are at `{tmp_path}/<skill>/…`; open one with the Read tool.")
        assert reach < result.index("--- SKILL: rules ---")


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


# --- harness_skills_dirs ---


def test_harness_skills_dirs_orders_local_shared_claude_and_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = tmp_path / "local"
    monkeypatch.setattr(skill_injection, "DEFAULT_SKILLS_DIR", local)
    monkeypatch.setattr(skill_injection.Path, "home", lambda: tmp_path)

    assert harness_skills_dirs() == [
        local,
        tmp_path / ".agents" / "skills",
        tmp_path / ".claude" / "skills",
        tmp_path / ".codex" / "skills",
    ]


def test_harness_skills_dirs_dedupes_identical_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shared = tmp_path / ".agents" / "skills"
    monkeypatch.setattr(skill_injection, "DEFAULT_SKILLS_DIR", shared)
    monkeypatch.setattr(skill_injection.Path, "home", lambda: tmp_path)

    assert harness_skills_dirs().count(shared) == 1


def test_read_skill_contents_falls_back_to_harness_dirs(tmp_path: Path, monkeypatch) -> None:
    # With no explicit skills_dir, the reader searches the harness dirs; a skill
    # seeded only in the patched DEFAULT dir must still resolve.
    seeded = tmp_path / "seeded"
    _write_skill(seeded, "harness-skill", "# harness body")
    monkeypatch.setattr(skill_injection, "DEFAULT_SKILLS_DIR", seeded)
    result = _read_skill_contents(["harness-skill"])
    assert "# harness body" in result


def test_read_scoped_falls_back_to_harness_dirs(tmp_path: Path, monkeypatch) -> None:
    seeded = tmp_path / "seeded"
    _write_skill(seeded, "scoped-skill", "# scoped body")
    monkeypatch.setattr(skill_injection, "DEFAULT_SKILLS_DIR", seeded)
    result = _read_skill_contents_scoped(["scoped-skill"], primary_skills={"scoped-skill"})
    assert "# scoped body" in result


def test_read_skill_contents_falls_back_to_codex_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = tmp_path / "local"
    codex = tmp_path / ".codex" / "skills"
    _write_skill(codex, "codex-only", "# codex body")
    monkeypatch.setattr(skill_injection, "DEFAULT_SKILLS_DIR", local)
    monkeypatch.setattr(skill_injection.Path, "home", lambda: tmp_path)

    assert "# codex body" in _read_skill_contents(["codex-only"])
