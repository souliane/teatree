from pathlib import Path

from typer.testing import CliRunner

from teatree.cli import app

runner = CliRunner()


def test_startoverlay_creates_lightweight_package(tmp_path: Path) -> None:
    result = runner.invoke(app, ["startoverlay", "t3-acme", str(tmp_path)])
    assert result.exit_code == 0, result.output

    project_root = tmp_path / "t3-acme"
    # Lightweight overlay structure
    assert (project_root / "src" / "t3_overlay" / "__init__.py").is_file()
    assert (project_root / "src" / "t3_overlay" / "overlay.py").is_file()
    assert (project_root / "src" / "t3_overlay" / "apps.py").is_file()
    assert (project_root / "skills" / "t3-overlay" / "SKILL.md").is_file()
    assert (project_root / "pyproject.toml").is_file()
    assert (project_root / ".editorconfig").is_file()
    assert (project_root / ".gitignore").is_file()
    assert (project_root / ".github" / "workflows" / "ci.yml").is_file()

    # No Django project files
    assert not (project_root / "manage.py").exists()
    assert not (project_root / "src" / "acme" / "settings.py").exists()
    assert not (project_root / "src" / "acme" / "urls.py").exists()

    # Overlay class is correct
    overlay_text = (project_root / "src" / "t3_overlay" / "overlay.py").read_text()
    assert "class T3OverlayOverlay(OverlayBase):" in overlay_text
    assert 'django_app: str | None = "t3_overlay"' in overlay_text

    # pyproject.toml has entry point
    pyproject_text = (project_root / "pyproject.toml").read_text()
    assert 'name = "t3-acme"' in pyproject_text


def test_startoverlay_with_explicit_options(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["startoverlay", "t3-acme", str(tmp_path), "--overlay-app", "acme_overlay", "--project-package", "acme"],
    )
    assert result.exit_code == 0, result.output

    project_root = tmp_path / "t3-acme"
    assert (project_root / "src" / "acme_overlay" / "overlay.py").is_file()
    assert (project_root / "skills" / "t3-acme" / "SKILL.md").is_file()


def test_startoverlay_rejects_existing_destination(tmp_path: Path) -> None:
    existing = tmp_path / "demo_project"
    existing.mkdir()
    result = runner.invoke(app, ["startoverlay", "demo_project", str(tmp_path)])
    assert result.exit_code == 1
    assert "Destination already exists" in result.stdout
