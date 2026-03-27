import json
from pathlib import Path

import pytest
from django.core.management import call_command

from teetree.core.docgen import (
    build_overlay_doc_payload,
    build_skill_doc_payload,
    render_overlay_markdown,
    render_skill_markdown,
)
from teetree.skill_map import load_skill_delegation, parse_skill_delegation_map, render_skill_delegation_map


def test_overlay_doc_payload_and_markdown_are_stable() -> None:
    payload = build_overlay_doc_payload()
    markdown = render_overlay_markdown(payload)

    assert payload["overlay_base"] == "teetree.core.overlay.OverlayBase"
    assert payload["hooks"][0]["name"] == "get_repos"
    assert payload["hooks"][0]["required"] is True
    assert "Overlay Extension Points" in markdown
    assert "`get_skill_metadata`" in markdown
    assert "`TEATREE_OVERLAY_CLASS`" in markdown


def test_parse_skill_delegation_map_preserves_sections() -> None:
    mapping = parse_skill_delegation_map(
        "# Skill Delegation\n\n## coding\n\n- test-driven-development\n- verification-before-completion\n",
    )

    assert mapping == {"coding": ["test-driven-development", "verification-before-completion"]}


def test_skill_doc_payload_and_markdown_include_runtime_fields() -> None:
    payload = build_skill_doc_payload(Path("references/skill-delegation.md"))
    markdown = render_skill_markdown(payload)

    assert payload["delegation"]["coding"] == ["test-driven-development", "verification-before-completion"]
    assert "Task claiming, leasing, and execution routing" in markdown
    assert "`framework_skills`" in markdown
    assert "`lifecycle_skill`" in markdown


def test_skill_doc_payload_uses_builtin_default_when_cwd_has_no_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    payload = build_skill_doc_payload(Path("references/skill-delegation.md"))

    assert payload["skill_map_path"] == "teetree.skill_map.DEFAULT_SKILL_DELEGATION"
    assert payload["delegation"]["shipping"] == [
        "finishing-a-development-branch",
        "verification-before-completion",
    ]


def test_generate_doc_commands_write_deterministic_outputs(tmp_path: Path) -> None:
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
    assert overlay_payload["hooks"][-1]["name"] == "get_skill_metadata"
    assert skill_payload["delegation"]["reviewing"] == [
        "requesting-code-review",
        "verification-before-completion",
    ]


def test_generate_skill_docs_command_uses_builtin_default_when_local_map_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    output_dir = tmp_path / "generated"

    call_command("generate_skill_docs", output_dir=str(output_dir))

    payload = json.loads((output_dir / "skill-delegation-matrix.json").read_text(encoding="utf-8"))

    assert payload["skill_map_path"] == "teetree.skill_map.DEFAULT_SKILL_DELEGATION"


def test_load_skill_delegation_raises_for_missing_non_default_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_skill_delegation(tmp_path / "missing-skill-map.md")


def test_load_skill_delegation_reads_existing_custom_file(tmp_path: Path) -> None:
    skill_map = tmp_path / "skill-delegation.md"
    skill_map.write_text(
        "# Skill Delegation\n\n## custom-phase\n\n- ac-custom-skill\n",
        encoding="utf-8",
    )

    source, delegation = load_skill_delegation(skill_map)

    assert source == str(skill_map)
    assert delegation == {"custom-phase": ["ac-custom-skill"]}


def test_render_skill_delegation_map_matches_builtin_shape() -> None:
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
