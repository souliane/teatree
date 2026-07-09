"""Tests for teatree.skill_support.ref_validator — dangling skill-reference detection.

Mirrors the real ``ac-reviewing-skills`` → ``ac-reviewing-codebase`` incident:
a ``$HOME/.teatree-skills.yml`` keyword→skill routing entry named a skill that
does not exist in the canonical (installed/remote) skill set. The validator
enumerates every reference site, resolves each name against the canonical
set, and flags any name that does not resolve — naming file:line, the bad
name, and the nearest valid matches.
"""

import os
from pathlib import Path

import pytest

from teatree.skill_support.ref_validator import (
    DanglingReference,
    canonical_skill_names,
    default_search_dirs,
    main,
    validate_agent_frontmatter,
    validate_repo_refs,
    validate_skill_refs,
    validate_supplementary_config,
)


def _seed_canonical(root: Path, names: list[str]) -> Path:
    """Create a skills tree with one ``<name>/SKILL.md`` per *name*."""
    for name in names:
        skill_dir = root / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: d\n---\n# {name}",
            encoding="utf-8",
        )
    return root


class TestDefaultSearchDirs:
    def test_override_env_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", f"{a}{os.pathsep}{b}")
        assert default_search_dirs() == [a, b]

    def test_default_includes_plugin_skills_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_SKILL_SEARCH_DIRS", raising=False)
        dirs = default_search_dirs()
        assert any(d.name == "skills" for d in dirs)


class TestCanonicalSkillNames:
    def test_enumerates_skill_dirs_not_hardcoded(self, tmp_path: Path) -> None:
        _seed_canonical(tmp_path, ["ac-reviewing-codebase", "ac-django", "code"])
        names = canonical_skill_names([tmp_path])
        assert names == {"ac-reviewing-codebase", "ac-django", "code"}

    def test_directory_without_skill_md_is_not_a_skill(self, tmp_path: Path) -> None:
        (tmp_path / "not-a-skill").mkdir()
        _seed_canonical(tmp_path, ["code"])
        names = canonical_skill_names([tmp_path])
        assert names == {"code"}

    def test_merges_multiple_search_dirs(self, tmp_path: Path) -> None:
        plugin = _seed_canonical(tmp_path / "plugin", ["rules", "code"])
        installed = _seed_canonical(tmp_path / "installed", ["ac-django"])
        names = canonical_skill_names([plugin, installed])
        assert names == {"rules", "code", "ac-django"}

    def test_missing_dir_skipped(self, tmp_path: Path) -> None:
        present = _seed_canonical(tmp_path / "present", ["code"])
        assert canonical_skill_names([tmp_path / "absent", present]) == {"code"}

    def test_unreadable_dir_skipped(self, tmp_path: Path) -> None:
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        present = _seed_canonical(tmp_path / "present", ["code"])
        blocked.chmod(0o000)
        try:
            assert canonical_skill_names([blocked, present]) == {"code"}
        finally:
            blocked.chmod(0o755)


class TestSupplementaryConfig:
    def _write_config(self, path: Path, body: str) -> Path:
        path.write_text(body, encoding="utf-8")
        return path

    def test_dangling_reference_flagged(self, tmp_path: Path) -> None:
        canonical = {"ac-reviewing-codebase", "ac-django", "ac-python"}
        config = self._write_config(
            tmp_path / ".teatree-skills.yml",
            "# routing\nac-reviewing-skills: '\\breview skills?\\b'\nac-django: '.'\n",
        )
        findings = validate_supplementary_config(config, canonical)
        bad = [f for f in findings if f.name == "ac-reviewing-skills"]
        assert len(bad) == 1
        assert bad[0].path == config
        assert bad[0].line == 2
        assert "ac-reviewing-codebase" in bad[0].suggestions

    def test_clean_config_passes(self, tmp_path: Path) -> None:
        canonical = {"ac-reviewing-codebase", "ac-django", "ac-python"}
        config = self._write_config(
            tmp_path / ".teatree-skills.yml",
            "ac-reviewing-codebase: '\\breview skills?\\b'\nac-django: '.'\nac-python: '.'\n",
        )
        assert validate_supplementary_config(config, canonical) == []

    def test_comments_and_blank_lines_ignored(self, tmp_path: Path) -> None:
        canonical = {"ac-django"}
        config = self._write_config(
            tmp_path / ".teatree-skills.yml",
            "# a comment naming ac-reviewing-skills should not be flagged\n\nac-django: '.'\n",
        )
        assert validate_supplementary_config(config, canonical) == []

    def test_missing_config_is_not_a_failure(self, tmp_path: Path) -> None:
        canonical = {"ac-django"}
        assert validate_supplementary_config(tmp_path / "absent.yml", canonical) == []

    def test_malformed_line_ignored(self, tmp_path: Path) -> None:
        config = self._write_config(
            tmp_path / ".teatree-skills.yml",
            "this line has no colon mapping\nac-django: '.'\n",
        )
        assert validate_supplementary_config(config, {"ac-django"}) == []

    def test_unreadable_config_fails_open(self, tmp_path: Path) -> None:
        config = self._write_config(tmp_path / ".teatree-skills.yml", "ac-django: '.'\n")
        config.chmod(0o000)
        try:
            assert validate_supplementary_config(config, {"ac-django"}) == []
        finally:
            config.chmod(0o644)


class TestAgentFrontmatter:
    def _write_agent(self, path: Path, skills: list[str]) -> Path:
        lines = ["---", "name: agent", "skills:"]
        lines.extend(f"  - {s}" for s in skills)
        lines.extend(["---", "# Agent"])
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def test_dangling_skill_flagged(self, tmp_path: Path) -> None:
        canonical = {"rules", "workspace", "code"}
        agent = self._write_agent(tmp_path / "coder.md", ["rules", "workspac", "code"])
        findings = validate_agent_frontmatter(agent, canonical)
        assert [f.name for f in findings] == ["workspac"]
        assert "workspace" in findings[0].suggestions

    def test_clean_agent_passes(self, tmp_path: Path) -> None:
        canonical = {"rules", "workspace", "code"}
        agent = self._write_agent(tmp_path / "coder.md", ["rules", "workspace", "code"])
        assert validate_agent_frontmatter(agent, canonical) == []

    def test_missing_agent_file_returns_empty(self, tmp_path: Path) -> None:
        assert validate_agent_frontmatter(tmp_path / "absent.md", {"rules"}) == []

    def test_frontmatter_without_closing_delimiter_scanned_to_end(self, tmp_path: Path) -> None:
        agent = tmp_path / "coder.md"
        agent.write_text("---\nname: coder\nskills:\n  - bogus\n", encoding="utf-8")
        findings = validate_agent_frontmatter(agent, {"rules"})
        assert [f.name for f in findings] == ["bogus"]

    def test_file_without_frontmatter_returns_empty(self, tmp_path: Path) -> None:
        agent = tmp_path / "doc.md"
        agent.write_text("# Just a heading\nskills:\n  - nonexistent\n", encoding="utf-8")
        assert validate_agent_frontmatter(agent, {"rules"}) == []

    def test_companion_skills_field_scanned(self, tmp_path: Path) -> None:
        agent = tmp_path / "coder.md"
        agent.write_text(
            "---\nname: coder\ncompanion_skills:\n  - bogus-companion\n---\n# C",
            encoding="utf-8",
        )
        findings = validate_agent_frontmatter(agent, {"rules"})
        assert [f.name for f in findings] == ["bogus-companion"]

    def test_unreadable_agent_fails_open(self, tmp_path: Path) -> None:
        agent = self._write_agent(tmp_path / "coder.md", ["rules"])
        agent.chmod(0o000)
        try:
            assert validate_agent_frontmatter(agent, {"workspace"}) == []
        finally:
            agent.chmod(0o644)


class TestValidateSkillRefs:
    def test_aggregates_across_sites_and_reports_red(self, tmp_path: Path) -> None:
        canonical_root = _seed_canonical(tmp_path / "skills", ["ac-reviewing-codebase", "ac-django"])
        config = tmp_path / ".teatree-skills.yml"
        config.write_text("ac-reviewing-skills: '\\breview\\b'\nac-django: '.'\n", encoding="utf-8")
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "coder.md").write_text(
            "---\nname: coder\nskills:\n  - ac-django\n---\n# C",
            encoding="utf-8",
        )
        findings = validate_skill_refs(
            search_dirs=[canonical_root],
            supplementary_config=config,
            agents_dir=agents_dir,
        )
        assert any(f.name == "ac-reviewing-skills" for f in findings)
        assert all(f.name != "ac-django" for f in findings)

    def test_clean_set_passes(self, tmp_path: Path) -> None:
        canonical_root = _seed_canonical(tmp_path / "skills", ["ac-reviewing-codebase", "ac-django"])
        config = tmp_path / ".teatree-skills.yml"
        config.write_text("ac-reviewing-codebase: '\\breview\\b'\n", encoding="utf-8")
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "coder.md").write_text(
            "---\nname: coder\nskills:\n  - ac-django\n---\n# C",
            encoding="utf-8",
        )
        assert (
            validate_skill_refs(
                search_dirs=[canonical_root],
                supplementary_config=config,
                agents_dir=agents_dir,
            )
            == []
        )

    def test_defaults_resolve_supplementary_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        canonical_root = _seed_canonical(tmp_path / "skills", ["ac-django"])
        config = tmp_path / "custom-skills.yml"
        config.write_text("bogus-skill: '.'\n", encoding="utf-8")
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        monkeypatch.setenv("T3_SUPPLEMENTARY_SKILLS", str(config))
        findings = validate_skill_refs(search_dirs=[canonical_root], agents_dir=agents_dir)
        assert [f.name for f in findings] == ["bogus-skill"]

    def test_defaults_search_dirs_and_agents(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        canonical_root = _seed_canonical(tmp_path / "skills", ["ac-django"])
        config = tmp_path / "empty.yml"
        config.write_text("ac-django: '.'\n", encoding="utf-8")
        monkeypatch.setattr(
            "teatree.skill_support.ref_validator.default_search_dirs",
            lambda: [canonical_root],
        )
        assert validate_skill_refs(supplementary_config=config, agents_dir=tmp_path / "no-agents") == []

    def test_default_agents_dir_is_the_repo_agents(self, tmp_path: Path) -> None:
        config = tmp_path / "empty.yml"
        config.write_text("rules: '.'\n", encoding="utf-8")
        findings = validate_skill_refs(supplementary_config=config)
        assert all(f.site != "supplementary-config" for f in findings)


class TestValidateRepoRefs:
    def _seed_repo(self, root: Path, skills: list[str], agent_skills: list[str]) -> Path:
        _seed_canonical(root / "skills", skills)
        agents = root / "agents"
        agents.mkdir(parents=True)
        lines = ["---", "name: coder", "skills:"]
        lines.extend(f"  - {s}" for s in agent_skills)
        lines.extend(["---", "# C"])
        (agents / "coder.md").write_text("\n".join(lines), encoding="utf-8")
        return root

    def test_clean_repo_passes(self, tmp_path: Path) -> None:
        repo = self._seed_repo(tmp_path, ["rules", "code"], ["rules", "code"])
        assert validate_repo_refs(repo) == []

    def test_dangling_agent_ref_flagged(self, tmp_path: Path) -> None:
        repo = self._seed_repo(tmp_path, ["rules", "code"], ["rules", "nonexistent"])
        findings = validate_repo_refs(repo)
        assert [f.name for f in findings] == ["nonexistent"]

    def test_namespaced_t3_prefix_resolves(self, tmp_path: Path) -> None:
        repo = self._seed_repo(tmp_path, ["rules", "code"], ["t3:rules", "code"])
        assert validate_repo_refs(repo) == []


class TestMain:
    def test_exits_nonzero_on_dangling_repo_ref(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_canonical(tmp_path / "skills", ["rules"])
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "coder.md").write_text("---\nname: coder\nskills:\n  - missing\n---\n# C", encoding="utf-8")
        monkeypatch.setattr(
            "teatree.skill_support.ref_validator.validate_repo_refs",
            lambda _root: validate_agent_frontmatter(agents / "coder.md", {"rules"}),
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    def test_clean_repo_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.skill_support.ref_validator.validate_repo_refs", lambda _root: [])
        main()

    def test_real_repo_path_passes(self) -> None:
        main()


class TestDanglingReferenceRendering:
    def test_render_names_file_line_and_suggestions(self) -> None:
        finding = DanglingReference(
            path=Path("/tmp/.teatree-skills.yml"),
            line=2,
            name="ac-reviewing-skills",
            site="supplementary-config",
            suggestions=["ac-reviewing-codebase"],
        )
        rendered = finding.render()
        assert "/tmp/.teatree-skills.yml:2" in rendered
        assert "ac-reviewing-skills" in rendered
        assert "ac-reviewing-codebase" in rendered
