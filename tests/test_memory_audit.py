"""Tests for teatree.memory_audit — scan memory files for promotable entries."""

from pathlib import Path

import pytest

from teatree.memory_audit import _detect_guardrail_patterns, _parse_frontmatter, _suggest_skill, scan_memory_dir


class TestParseFrontmatter:
    def test_parses_yaml_frontmatter(self) -> None:
        text = "---\nname: my-rule\ntype: feedback\n---\nBody here."
        fields, body = _parse_frontmatter(text)
        assert fields["name"] == "my-rule"
        assert fields["type"] == "feedback"
        assert body == "Body here."

    def test_returns_empty_when_no_frontmatter(self) -> None:
        text = "Just a body with no frontmatter."
        fields, body = _parse_frontmatter(text)
        assert fields == {}
        assert body == text


class TestDetectGuardrailPatterns:
    @pytest.mark.parametrize(
        "text",
        [
            "NEVER run this command directly.",
            "You must ALWAYS check first.",
            "This is non-negotiable.",
            "Do NOT use pip on local.",
        ],
    )
    def test_detects_guardrail_language(self, text: str) -> None:
        assert len(_detect_guardrail_patterns(text)) > 0

    def test_returns_empty_for_neutral_text(self) -> None:
        assert _detect_guardrail_patterns("The user prefers Emacs.") == ()


class TestSuggestSkill:
    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("Never push without explicit approval", "ship"),
            ("Always run the test suite first", "test"),
            ("When reviewing code, check for N+1", "review"),
            ("Worktree must be isolated", "workspace"),
            ("Generic guardrail about something", "rules"),
        ],
    )
    def test_maps_keywords_to_skills(self, body: str, expected: str) -> None:
        assert _suggest_skill(body) == expected


class TestScanMemoryDir:
    def test_scans_and_flags_promotable_entries(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "MEMORY.md").write_text("# Index\n")
        (memory_dir / "feedback_push.md").write_text(
            "---\nname: push-rules\ntype: feedback\n---\nNEVER push without explicit approval from the user."
        )
        (memory_dir / "user_editor.md").write_text("---\nname: editor\ntype: user\n---\nUser prefers Emacs.")

        entries = scan_memory_dir(memory_dir)
        assert len(entries) == 1
        assert entries[0].name == "push-rules"
        assert entries[0].suggested_skill == "ship"

    def test_skips_memory_index(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "MEMORY.md").write_text("NEVER skip this.\n")

        entries = scan_memory_dir(memory_dir)
        assert entries == []
