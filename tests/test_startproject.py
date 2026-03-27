import os
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from teetree.cli import app

runner = CliRunner()


def test_startproject_with_defaults_uses_t3_overlay_app(tmp_path: Path) -> None:
    """t3 startproject t3-acme /dest → project_package=acme, overlay_app=t3_overlay."""
    result = runner.invoke(
        app,
        ["startproject", "t3-acme", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output

    project_root = tmp_path / "t3-acme"
    assert (project_root / "manage.py").is_file()
    assert (project_root / "src" / "acme" / "settings.py").is_file()
    assert (project_root / "src" / "acme" / "urls.py").is_file()
    assert (project_root / "src" / "t3_overlay" / "apps.py").is_file()
    assert (project_root / "src" / "t3_overlay" / "overlay.py").is_file()
    assert (project_root / "skills" / "t3-overlay" / "SKILL.md").is_file()
    assert (project_root / ".editorconfig").is_file()
    assert (project_root / ".gitignore").is_file()
    assert (project_root / ".markdownlint-cli2.yaml").is_file()
    assert (project_root / ".pre-commit-config.yaml").is_file()
    assert (project_root / ".python-version").is_file()
    assert (project_root / "pyproject.toml").is_file()

    pyproject_text = (project_root / "pyproject.toml").read_text()
    assert 'name = "t3-acme"' in pyproject_text
    assert '"t3_overlay", "acme"' in pyproject_text

    settings_text = (project_root / "src" / "acme" / "settings.py").read_text()
    assert 'TEATREE_OVERLAY_CLASS = "t3_overlay.overlay.T3OverlayOverlay"' in settings_text
    assert "'teetree.core'" in settings_text
    assert "'t3_overlay'" in settings_text

    urls_text = (project_root / "src" / "acme" / "urls.py").read_text()
    assert "include('teetree.core.urls')" in urls_text

    overlay_text = (project_root / "src" / "t3_overlay" / "overlay.py").read_text()
    assert '"skill_path": "skills/t3-overlay/SKILL.md"' in overlay_text

    skill_text = (project_root / "skills" / "t3-overlay" / "SKILL.md").read_text()
    assert "name: t3-overlay" in skill_text
    assert "requires:" not in skill_text
    assert "- t3-workspace" not in skill_text

    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    check = subprocess.run(  # noqa: S603
        [sys.executable, "manage.py", "check"],
        cwd=project_root,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert check.returncode == 0, check.stderr


def test_startproject_with_explicit_options(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "startproject",
            "t3-acme",
            str(tmp_path),
            "--project-package",
            "acme",
            "--overlay-app",
            "acme_overlay",
        ],
    )

    assert result.exit_code == 0, result.output

    project_root = tmp_path / "t3-acme"
    assert (project_root / "src" / "acme" / "settings.py").is_file()
    assert (project_root / "src" / "acme_overlay" / "overlay.py").is_file()
    assert (project_root / "skills" / "t3-acme" / "SKILL.md").is_file()

    pyproject_text = (project_root / "pyproject.toml").read_text()
    assert 'name = "t3-acme"' in pyproject_text
    assert '"acme_overlay", "acme"' in pyproject_text

    skill_text = (project_root / "skills" / "t3-acme" / "SKILL.md").read_text()
    assert "name: t3-acme" in skill_text


def test_startproject_golden_bootstrap(tmp_path: Path) -> None:
    """Full bootstrap golden test: generate project, verify structure, run check, verify .env."""
    result = runner.invoke(
        app,
        ["startproject", "test-project", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output

    project_root = tmp_path / "test-project"

    # Core project structure exists
    assert (project_root / "manage.py").is_file()
    assert (project_root / "src" / "test_project" / "settings.py").is_file()
    assert (project_root / "src" / "test_project" / "urls.py").is_file()
    assert (project_root / "src" / "t3_overlay" / "apps.py").is_file()
    assert (project_root / "src" / "t3_overlay" / "overlay.py").is_file()
    assert (project_root / "skills" / "t3-overlay" / "SKILL.md").is_file()
    assert (project_root / "pyproject.toml").is_file()
    assert (project_root / ".env").is_file()
    assert "DJANGO_SETTINGS_MODULE=test_project.settings" in (project_root / ".env").read_text()
    # Generated project passes Django's system checks
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    check = subprocess.run(  # noqa: S603
        [sys.executable, "manage.py", "check"],
        cwd=project_root,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert check.returncode == 0, check.stderr


def test_startproject_rejects_existing_destination(tmp_path: Path) -> None:
    existing = tmp_path / "demo_project"
    existing.mkdir()

    result = runner.invoke(
        app,
        ["startproject", "demo_project", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "Destination already exists" in result.stdout
