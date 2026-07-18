"""Overlay ``tool`` group registration resolves its root through the seam (#3355).

Before the fix a non-``<project>/skills`` layout matched zero files and the whole
``t3 <overlay> tool`` group vanished with no diagnostic. These prove the tool
group registers from a declared ``skill_root``, and that a declared-but-empty root
warns instead of returning silently.
"""

import json
import logging
from pathlib import Path

import pytest
import typer

from teatree.cli.overlay import OverlayAppBuilder


def _write_tool_commands(skills_root: Path) -> None:
    hook_dir = skills_root / "t3:demo" / "hook-config"
    hook_dir.mkdir(parents=True)
    (hook_dir / "tool-commands.json").write_text(
        json.dumps([{"name": "widget", "help": "Do a thing", "command": "widget_cmd"}]),
        encoding="utf-8",
    )


def _group_names(app: typer.Typer) -> set[str]:
    return {info.name for info in app.registered_groups if info.name}


def test_tool_group_registers_from_a_declared_skill_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    custom_root = tmp_path / "packaged" / "skills"
    _write_tool_commands(custom_root)

    monkeypatch.setattr(
        "teatree.core.overlay_skills.overlay_skill_metadata",
        lambda _name: {"skill_root": str(custom_root)},
    )

    builder = OverlayAppBuilder("t3-demo", project)
    builder._register_overlay_tools()
    assert "tool" in _group_names(builder.overlay_app)


def test_declared_root_without_tool_commands_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    empty_root = tmp_path / "packaged" / "skills"
    empty_root.mkdir(parents=True)

    monkeypatch.setattr(
        "teatree.core.overlay_skills.overlay_skill_metadata",
        lambda _name: {"skill_root": str(empty_root)},
    )

    builder = OverlayAppBuilder("t3-demo", project)
    with caplog.at_level(logging.WARNING, logger="teatree.cli.overlay"):
        builder._register_overlay_tools()

    assert "tool" not in _group_names(builder.overlay_app)
    assert any("skills root" in rec.message and str(empty_root) in rec.getMessage() for rec in caplog.records)


def test_default_layout_without_tools_stays_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # No declared root + no tools = an overlay that simply ships none; not a
    # misconfiguration, so no warning noise on every CLI invocation.
    project = tmp_path / "project"
    (project / "skills").mkdir(parents=True)

    monkeypatch.setattr("teatree.core.overlay_skills.overlay_skill_metadata", lambda _name: {})

    builder = OverlayAppBuilder("t3-demo", project)
    with caplog.at_level(logging.WARNING, logger="teatree.cli.overlay"):
        builder._register_overlay_tools()

    assert "tool" not in _group_names(builder.overlay_app)
    assert caplog.records == []
