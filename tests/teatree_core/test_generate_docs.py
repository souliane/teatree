import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

import teatree.core.docgen as docgen_mod
from teatree.core.docgen import (
    _extract_subcommands,
    build_cli_reference,
    build_overlay_doc_payload,
    build_skill_doc_payload,
    render_overlay_markdown,
    render_skill_markdown,
    write_generated_doc,
)
from teatree.skill_map import load_skill_delegation, parse_skill_delegation_map, render_skill_delegation_map


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

        assert payload["skill_map_path"] == "teatree.skill_map.DEFAULT_SKILL_DELEGATION"
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

        assert payload["skill_map_path"] == "teatree.skill_map.DEFAULT_SKILL_DELEGATION"


class TestExtractSubcommands:
    def test_parses_click_format(self) -> None:
        help_text = """\
Usage: t3 [OPTIONS] COMMAND [ARGS]...

Commands:
    dashboard   Start the dashboard
    config      Configuration and autoloading
    doctor      Health checks
"""
        commands = _extract_subcommands(help_text)
        assert commands == ["dashboard", "config", "doctor"]

    def test_parses_typer_box_format(self) -> None:
        help_text = """\
Usage: t3 [OPTIONS] COMMAND [ARGS]...

╭─ Commands ─────────────────────────────────╮
│ dashboard   Start the dashboard             │
│ config      Configuration and autoloading   │
╰─────────────────────────────────────────────╯
"""
        commands = _extract_subcommands(help_text)
        assert commands == ["dashboard", "config"]

    def test_returns_empty_for_no_commands(self) -> None:
        help_text = "Usage: t3 [OPTIONS]\n\nSome description."
        commands = _extract_subcommands(help_text)
        assert commands == []


class TestRunHelp:
    def test_returns_stdout(self) -> None:
        with patch.object(
            docgen_mod.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "Usage: t3 --help", ""),
        ):
            result = docgen_mod._run_help(["t3"])
        assert result == "Usage: t3 --help"


class TestBuildCliReference:
    def test_generates_markdown(self) -> None:
        def mock_run_help(cmd: list[str]) -> str:
            if cmd == ["t3"]:
                return "Usage: t3\n\nCommands:\n  config   Config commands"
            if cmd == ["t3", "config"]:
                return "Usage: t3 config\n\nNo subcommands."
            return ""

        with patch.object(docgen_mod, "_run_help", side_effect=mock_run_help):
            result = build_cli_reference(["t3"])
        assert "# CLI Reference" in result
        assert "`t3`" in result
        assert "`t3 config`" in result


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


class TestGenerateCliDocsCommand:
    def test_writes_cli_reference(self, tmp_path: Path) -> None:
        output = tmp_path / "cli-reference.md"
        with patch.object(docgen_mod, "build_cli_reference", return_value="# CLI Reference\n"):
            call_command("generate_cli_docs", output=str(output))
        assert output.is_file()
        assert output.read_text(encoding="utf-8") == "# CLI Reference\n"


class TestGenerateAllDocsCommand:
    def test_generates_all_three_docs(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "generated"
        with patch.object(docgen_mod, "build_cli_reference", return_value="# CLI\n"):
            call_command("generate_all_docs", output_dir=str(output_dir))
        assert (output_dir / "overlay-extension-points.md").is_file()
        assert (output_dir / "skill-delegation-matrix.md").is_file()
        assert (output_dir / "cli-reference.md").is_file()
