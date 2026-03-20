"""Tests for teetree.scaffold.bootstrap — the standalone scaffold CLI."""

from pathlib import Path

from typer.testing import CliRunner

from teetree.scaffold.bootstrap import _camelize, _render_template, app

runner = CliRunner()


# ── _render_template ──────────────────────────────────────────────────


def test_render_template_replaces_placeholders():
    template = "Hello {{ name }}, welcome to {{ project }}!"
    result = _render_template(template, {"name": "Alice", "project": "Tea"})
    assert result == "Hello Alice, welcome to Tea!"


def test_render_template_no_placeholders():
    template = "Nothing to replace here."
    result = _render_template(template, {"name": "Alice"})
    assert result == "Nothing to replace here."


def test_render_template_empty_context():
    template = "{{ name }} stays"
    result = _render_template(template, {})
    assert result == "{{ name }} stays"


# ── _camelize ─────────────────────────────────────────────────────────


def test_camelize_single_word():
    assert _camelize("overlay") == "Overlay"


def test_camelize_underscored_name():
    assert _camelize("my_cool_overlay") == "MyCoolOverlay"


def test_camelize_empty_string():
    assert _camelize("") == ""


# ── startproject command ──────────────────────────────────────────────


def test_startproject_creates_project_structure(tmp_path: Path):
    result = runner.invoke(
        app,
        ["startproject", "demo-project", str(tmp_path), "--overlay-app", "demo_overlay"],
    )

    assert result.exit_code == 0, result.output
    project_root = tmp_path / "demo-project"
    assert project_root.is_dir()
    assert (project_root / "manage.py").is_file()
    assert (project_root / "src" / "demo_project" / "settings.py").is_file()
    assert (project_root / "src" / "demo_overlay" / "overlay.py").is_file()
    assert (project_root / "skills" / "t3-demo" / "SKILL.md").is_file()


def test_startproject_with_explicit_package(tmp_path: Path):
    result = runner.invoke(
        app,
        [
            "startproject",
            "my-project",
            str(tmp_path),
            "--overlay-app",
            "my_overlay",
            "--project-package",
            "mypkg",
        ],
    )

    assert result.exit_code == 0, result.output
    project_root = tmp_path / "my-project"
    assert (project_root / "src" / "mypkg" / "settings.py").is_file()


def test_startproject_rejects_existing_destination(tmp_path: Path):
    (tmp_path / "existing").mkdir()
    result = runner.invoke(
        app,
        ["startproject", "existing", str(tmp_path), "--overlay-app", "t3_overlay"],
    )
    assert result.exit_code == 1
    assert "Destination already exists" in result.output


def test_bootstrap_callback():
    """The callback returns None (no-op)."""
    from teetree.scaffold.bootstrap import bootstrap  # noqa: PLC0415

    assert bootstrap() is None
