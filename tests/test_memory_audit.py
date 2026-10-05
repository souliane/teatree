"""Tests for teatree.memory_audit — scan memory files for promotable entries."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.memory_audit import (
    _detect_guardrail_patterns,
    _parse_frontmatter,
    _suggest_skill,
    discover_memory_dirs,
    scan_memory_dir,
    transcript_mount,
)


def test_transcript_mount_reads_the_live_bind_even_when_config_says_volume() -> None:
    root = Path("/home/teatree/.claude/projects")
    mountinfo = f"123 456 0:1 /srv/owner-sessions {root} rw - ext4 /dev/sda rw\n"
    with (
        patch.dict(os.environ, {"TEATREE_TRANSCRIPT_SOURCE": "teatree_claude_projects"}),
        patch("teatree.memory_audit.Path.read_text", return_value=mountinfo),
    ):
        assert transcript_mount(root) == ("bind", "/srv/owner-sessions")


def test_transcript_mount_without_config_or_mount_is_unknown() -> None:
    root = Path("/home/teatree/.claude/projects")
    with (
        patch.dict(os.environ, {}, clear=True),
        patch("teatree.memory_audit.Path.read_text", return_value=""),
    ):
        assert transcript_mount(root) == ("unknown", "")


@pytest.mark.parametrize("volume_name", ["teatree_claude_projects", "teatree_teatree_claude_projects"])
def test_transcript_mount_recognizes_the_factory_named_volume(volume_name: str) -> None:
    root = Path("/home/teatree/.claude/projects")
    volume_root = f"/var/lib/docker/volumes/{volume_name}/_data"
    mountinfo = f"123 456 0:1 {volume_root} {root} rw - ext4 /dev/sda rw\n"
    with (
        patch.dict(os.environ, {"TEATREE_TRANSCRIPT_SOURCE": "teatree_claude_projects"}),
        patch("teatree.memory_audit.Path.read_text", return_value=mountinfo),
    ):
        assert transcript_mount(root) == ("volume", volume_name)


def test_discovery_rejects_memory_symlink_outside_factory_root(tmp_path: Path) -> None:
    projects = tmp_path / ".claude" / "projects"
    project = projects / "project"
    project.mkdir(parents=True)
    outside = tmp_path / "owner-memory"
    outside.mkdir()
    (project / "memory").symlink_to(outside, target_is_directory=True)
    with (
        patch.dict(os.environ, {"TEATREE_TRANSCRIPT_SOURCE": "teatree_claude_projects"}),
        patch("teatree.memory_audit.Path.home", return_value=tmp_path),
    ):
        assert discover_memory_dirs(writable_only=True) == []


def test_host_bound_memory_is_readable_but_not_a_dream_write_target(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".claude" / "projects" / "project" / "memory"
    memory_dir.mkdir(parents=True)
    with (
        patch.dict(os.environ, {"TEATREE_TRANSCRIPT_SOURCE": "/srv/owner-sessions"}),
        patch("teatree.memory_audit.Path.home", return_value=tmp_path),
    ):
        assert discover_memory_dirs() == [memory_dir]
        assert discover_memory_dirs(writable_only=True) == []


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
