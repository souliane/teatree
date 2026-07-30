import json
from pathlib import Path

import pytest
from django.core.management import call_command

from teatree.core.evidence.doc_render import (
    build_overlay_doc_payload,
    build_skill_catalogue_payload,
    build_skill_doc_payload,
    render_overlay_markdown,
    render_skill_catalogue_markdown,
    render_skill_markdown,
    write_generated_doc,
)
from teatree.skill_support.map import load_skill_delegation, parse_skill_delegation_map, render_skill_delegation_map


class TestOverlayDocs:
    def test_payload_and_markdown_are_stable(self) -> None:
        payload = build_overlay_doc_payload()
        markdown = render_overlay_markdown(payload)

        assert payload["overlay_base"] == "teatree.core.overlay.OverlayBase"
        assert payload["hooks"][0]["name"] == "get_repos"
        assert payload["hooks"][0]["required"] is True
        assert "Overlay Extension Points" in markdown
        assert "`metadata.get_skill_metadata`" in markdown
        assert "## Settings" in markdown


class TestSkillDelegationMap:
    def test_parse_preserves_sections(self) -> None:
        mapping = parse_skill_delegation_map(
            "# Skill Delegation\n\n## coding\n\n- test-driven-development\n- verification-before-completion\n",
        )

        assert mapping == {"coding": ["test-driven-development", "verification-before-completion"]}

    def test_render_matches_builtin_shape(self) -> None:
        markdown = render_skill_delegation_map(
            {
                "coding": ("test-driven-development", "verification-before-completion"),
                "ticket-intake": ("writing-plans",),
            },
        )

        assert markdown == (
            "# Skill Delegation\n\n"
            "## coding\n\n"
            "- test-driven-development\n"
            "- verification-before-completion\n\n"
            "## ticket-intake\n\n"
            "- writing-plans\n"
        )


class TestSkillDocPayload:
    def test_includes_runtime_fields(self) -> None:
        payload = build_skill_doc_payload(Path("references/skill-delegation.md"))
        markdown = render_skill_markdown(payload)

        assert payload["delegation"]["coding"] == ["test-driven-development", "verification-before-completion"]
        assert "Task claiming, leasing, and execution routing" in markdown
        assert "`framework_skills`" in markdown
        assert "`lifecycle_skill`" in markdown

    def test_uses_builtin_default_when_cwd_has_no_references(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)

        payload = build_skill_doc_payload(Path("references/skill-delegation.md"))

        assert payload["skill_map_path"] == "teatree.skill_support.map.DEFAULT_SKILL_DELEGATION"
        assert payload["delegation"]["shipping"] == [
            "finishing-a-development-branch",
            "verification-before-completion",
        ]


class TestLoadSkillDelegation:
    def test_raises_for_missing_non_default_path(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_skill_delegation(tmp_path / "missing-skill-map.md")

    def test_reads_existing_custom_file(self, tmp_path: Path) -> None:
        skill_map = tmp_path / "skill-delegation.md"
        skill_map.write_text(
            "# Skill Delegation\n\n## custom-phase\n\n- ac-custom-skill\n",
            encoding="utf-8",
        )

        source, delegation = load_skill_delegation(skill_map)

        assert source == str(skill_map)
        assert delegation == {"custom-phase": ["ac-custom-skill"]}


class TestGenerateDocCommands:
    def test_write_deterministic_outputs(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "generated"

        call_command("generate_overlay_docs", output_dir=str(output_dir))
        call_command("generate_skill_docs", output_dir=str(output_dir))
        first_overlay_json = (output_dir / "overlay-extension-points.json").read_text(encoding="utf-8")
        first_skill_json = (output_dir / "skill-delegation-matrix.json").read_text(encoding="utf-8")

        call_command("generate_overlay_docs", output_dir=str(output_dir))
        call_command("generate_skill_docs", output_dir=str(output_dir))

        overlay_payload = json.loads((output_dir / "overlay-extension-points.json").read_text(encoding="utf-8"))
        skill_payload = json.loads((output_dir / "skill-delegation-matrix.json").read_text(encoding="utf-8"))
        overlay_markdown = (output_dir / "overlay-extension-points.md").read_text(encoding="utf-8")
        skill_markdown = (output_dir / "skill-delegation-matrix.md").read_text(encoding="utf-8")

        assert overlay_markdown.startswith("# Overlay Extension Points")
        assert skill_markdown.startswith("# Skill Delegation Matrix")
        assert first_overlay_json == (output_dir / "overlay-extension-points.json").read_text(encoding="utf-8")
        assert first_skill_json == (output_dir / "skill-delegation-matrix.json").read_text(encoding="utf-8")
        assert overlay_payload["hooks"][-1]["name"] == "metadata.get_followup_repos"
        assert skill_payload["delegation"]["reviewing"] == [
            "requesting-code-review",
            "verification-before-completion",
        ]

    def test_uses_builtin_default_when_local_map_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        output_dir = tmp_path / "generated"

        call_command("generate_skill_docs", output_dir=str(output_dir))

        payload = json.loads((output_dir / "skill-delegation-matrix.json").read_text(encoding="utf-8"))

        assert payload["skill_map_path"] == "teatree.skill_support.map.DEFAULT_SKILL_DELEGATION"


class TestSkillCatalogue:
    def test_payload_reads_frontmatter(self, tmp_path: Path) -> None:
        skills_root = tmp_path / "skills"
        (skills_root / "alpha").mkdir(parents=True)
        (skills_root / "alpha" / "SKILL.md").write_text(
            '---\nname: alpha\ndescription: Does alpha things. Use when user says "alpha".\n---\n# Alpha\n',
            encoding="utf-8",
        )
        (skills_root / "beta").mkdir(parents=True)
        (skills_root / "beta" / "SKILL.md").write_text(
            "---\nname: beta\ndescription: Does beta things.\n---\n# Beta\n",
            encoding="utf-8",
        )

        payload = build_skill_catalogue_payload(skills_root)

        assert payload["skills_root"] == "skills"
        assert payload["skills"] == [
            {"name": "alpha", "summary": "Does alpha things"},
            {"name": "beta", "summary": "Does beta things"},
        ]

    def test_payload_falls_back_to_dir_name_without_frontmatter(self, tmp_path: Path) -> None:
        skills_root = tmp_path / "skills"
        (skills_root / "gamma").mkdir(parents=True)
        (skills_root / "gamma" / "SKILL.md").write_text("# Gamma\nNo frontmatter here.\n", encoding="utf-8")

        payload = build_skill_catalogue_payload(skills_root)

        assert payload["skills"] == [{"name": "gamma", "summary": ""}]

    def test_markdown_is_a_stable_table(self) -> None:
        payload = {
            "skills_root": "skills",
            "skills": [
                {"name": "alpha", "summary": "Does alpha things"},
                {"name": "beta", "summary": "Does beta things"},
            ],
        }

        markdown = render_skill_catalogue_markdown(payload)

        assert markdown == (
            "# Skills Catalogue\n\n"
            "Source: `skills/*/SKILL.md` frontmatter\n\n"
            "| Skill | Summary |\n"
            "| --- | --- |\n"
            "| `alpha` | Does alpha things |\n"
            "| `beta` | Does beta things |\n"
        )

    def test_summary_escapes_table_pipes(self) -> None:
        markdown = render_skill_catalogue_markdown(
            {"skills_root": "skills", "skills": [{"name": "x", "summary": "a | b"}]},
        )

        assert "| `x` | a \\| b |" in markdown

    def test_management_command_writes_catalogue(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "generated"

        call_command("generate_skill_catalogue", output_dir=str(output_dir))

        markdown = (output_dir / "skills-catalogue.md").read_text(encoding="utf-8")
        payload = json.loads((output_dir / "skills-catalogue.json").read_text(encoding="utf-8"))
        assert markdown.startswith("# Skills Catalogue")
        assert payload["skills_root"] == "skills"
        names = {entry["name"] for entry in payload["skills"]}
        assert "code" in names

    def test_generate_all_docs_includes_catalogue(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "generated"
        call_command("generate_all_docs", output_dir=str(output_dir))
        assert (output_dir / "skills-catalogue.md").is_file()


class TestWriteGeneratedDoc:
    def test_creates_files(self, tmp_path: Path) -> None:
        json_path = tmp_path / "out" / "data.json"
        md_path = tmp_path / "out" / "doc.md"
        payload = build_overlay_doc_payload()
        markdown = render_overlay_markdown(payload)
        write_generated_doc(json_path, md_path, payload, markdown)
        assert json_path.is_file()
        assert md_path.is_file()
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["overlay_base"] == "teatree.core.overlay.OverlayBase"


class TestGenerateAllDocsCommand:
    def test_generates_overlay_and_skill_docs(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "generated"
        call_command("generate_all_docs", output_dir=str(output_dir))
        assert (output_dir / "overlay-extension-points.md").is_file()
        assert (output_dir / "skill-delegation-matrix.md").is_file()
