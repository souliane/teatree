"""Overlay ``tool`` group registration resolves its root through the seam (#3355).

A non-``<project>/skills`` layout matched zero files and the whole ``t3 <overlay>
tool`` group vanished with no diagnostic. These prove the tool group registers
from a declared ``skill_root``, and that the missing-manifest warning is keyed on
the overlay DECLARING tool commands (``get_tool_commands()``) rather than on
``skill_root`` — which says only where an overlay's skills live (#3904, #3915).
"""

import json
import logging
from pathlib import Path

import pytest
import typer

from teatree.cli.overlay import OverlayAppBuilder
from teatree.types import SkillMetadata, ToolCommand


def _write_tool_commands(skills_root: Path) -> None:
    hook_dir = skills_root / "t3:demo" / "hook-config"
    hook_dir.mkdir(parents=True)
    (hook_dir / "tool-commands.json").write_text(
        json.dumps([{"name": "widget", "help": "Do a thing", "command": "widget_cmd"}]),
        encoding="utf-8",
    )


def _patch_overlay(
    monkeypatch: pytest.MonkeyPatch,
    *,
    skill_metadata: SkillMetadata,
    tool_commands: list[ToolCommand],
) -> None:
    """Stand a stub overlay behind ``get_overlay`` — the one seam both resolvers use."""

    class _Metadata:
        def get_skill_metadata(self) -> SkillMetadata:
            return skill_metadata

        def get_tool_commands(self) -> list[ToolCommand]:
            return tool_commands

    class _Overlay:
        metadata = _Metadata()

    monkeypatch.setattr("teatree.core.overlay_loader.get_overlay", lambda _name=None: _Overlay())


def _group_names(app: typer.Typer) -> set[str]:
    return {info.name for info in app.registered_groups if info.name}


def test_tool_group_registers_from_a_declared_skill_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    custom_root = tmp_path / "packaged" / "skills"
    _write_tool_commands(custom_root)

    _patch_overlay(monkeypatch, skill_metadata={"skill_root": str(custom_root)}, tool_commands=[])

    builder = OverlayAppBuilder("t3-demo", project)
    builder._register_overlay_tools()
    assert "tool" in _group_names(builder.overlay_app)


def test_declared_skill_root_without_tool_commands_stays_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The shipped shape: an overlay declares where its SKILLS live and exposes no
    # tool commands. That is not a misconfiguration, so it must not warn on every
    # single CLI invocation (#3904, #3915).
    project = tmp_path / "project"
    project.mkdir()
    skills_root = tmp_path / "packaged" / "skills"
    skills_root.mkdir(parents=True)

    _patch_overlay(monkeypatch, skill_metadata={"skill_root": str(skills_root)}, tool_commands=[])

    builder = OverlayAppBuilder("t3-demo", project)
    with caplog.at_level(logging.WARNING, logger="teatree.cli.overlay"):
        builder._register_overlay_tools()

    assert "tool" not in _group_names(builder.overlay_app)
    assert caplog.records == []


def test_declared_tool_commands_without_manifest_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The genuine misconfiguration: the overlay declares a tool surface, but no
    # manifest registers it, so the documented commands silently do not exist.
    project = tmp_path / "project"
    (project / "skills").mkdir(parents=True)

    _patch_overlay(
        monkeypatch,
        skill_metadata={},
        tool_commands=[{"name": "widget", "help": "Do a thing", "command": "widget_cmd"}],
    )

    builder = OverlayAppBuilder("t3-demo", project)
    with caplog.at_level(logging.WARNING, logger="teatree.cli.overlay"):
        builder._register_overlay_tools()

    assert "tool" not in _group_names(builder.overlay_app)
    assert any("declares tool commands" in rec.getMessage() for rec in caplog.records)
    assert any(str(project / "skills") in rec.getMessage() for rec in caplog.records)


def test_default_layout_without_tools_stays_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # No declared root + no tools = an overlay that simply ships none; not a
    # misconfiguration, so no warning noise on every CLI invocation.
    project = tmp_path / "project"
    (project / "skills").mkdir(parents=True)

    _patch_overlay(monkeypatch, skill_metadata={}, tool_commands=[])

    builder = OverlayAppBuilder("t3-demo", project)
    with caplog.at_level(logging.WARNING, logger="teatree.cli.overlay"):
        builder._register_overlay_tools()

    assert "tool" not in _group_names(builder.overlay_app)
    assert caplog.records == []
