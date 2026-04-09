"""Lightweight overlay package scaffolder.

Generates a pure Python package that registers as a teatree overlay
via entry points. No Django project files — teatree is the Django project.
"""

from pathlib import Path
from textwrap import dedent


def camelize(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


class OverlayScaffolder:
    """Generate a TeaTree overlay package."""

    def __init__(self, project_root: Path, overlay_app: str, package_name: str) -> None:
        self.project_root = project_root
        self.overlay_app = overlay_app
        self.package_name = package_name
        self.overlay_class_name = camelize(overlay_app)
        self.src_dir = project_root / "src"

    def write_overlay(self, skill_name: str) -> None:
        pkg_dir = self.src_dir / self.overlay_app
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
        (pkg_dir / "overlay.py").write_text(
            dedent(f"""\
                from teatree.core.overlay import OverlayBase


                class {self.overlay_class_name}Overlay(OverlayBase):
                    django_app: str | None = "{self.overlay_app}"

                    def get_repos(self) -> list[str]:
                        return []

                    def get_provision_steps(self, worktree):
                        return []

                    def get_skill_metadata(self):
                        return {{"skill_path": "skills/{skill_name}/SKILL.md"}}
            """),
            encoding="utf-8",
        )
        (pkg_dir / "apps.py").write_text(
            dedent(f"""\
                from django.apps import AppConfig


                class {self.overlay_class_name}Config(AppConfig):
                    default_auto_field = "django.db.models.BigAutoField"
                    name = "{self.overlay_app}"
            """),
            encoding="utf-8",
        )

    @staticmethod
    def write_skill_md(skill_dir: Path, project_name: str, skill_name: str) -> None:
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            dedent(f"""\
                ---
                name: {skill_name}
                description: Project overlay skill for {project_name}.
                metadata:
                    version: 0.0.1
                ---

                # {skill_name}

                Project overlay skill companion for {project_name}.
            """),
            encoding="utf-8",
        )

    def copy_config_templates(self) -> None:
        from importlib.resources import files  # noqa: PLC0415

        template_dir = files("teatree.templates").joinpath("overlay")
        templates = {
            ".editorconfig": ".editorconfig",
            ".gitignore": ".gitignore",
            ".markdownlint-cli2.yaml": ".markdownlint-cli2.yaml",
            ".pre-commit-config.yaml.tmpl": ".pre-commit-config.yaml",
            ".python-version": ".python-version",
            "ci.yml.tmpl": ".github/workflows/ci.yml",
        }
        for source_name, dest_name in templates.items():
            source = template_dir.joinpath(source_name)
            dest = self.project_root / dest_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    def write_pyproject(self, project_name: str) -> None:
        from importlib.resources import files  # noqa: PLC0415

        template = (
            files("teatree.templates").joinpath("overlay").joinpath("pyproject.toml.tmpl").read_text(encoding="utf-8")
        )
        content = template.replace("{{project_name}}", project_name)
        content = content.replace("{{overlay_app}}", self.overlay_app)
        content = content.replace("{{package_name}}", self.package_name)
        content = content.replace("{{description}}", f"TeaTree overlay for {self.overlay_class_name}")
        self.project_root.joinpath("pyproject.toml").write_text(content, encoding="utf-8")

    def scaffold(self, project_name: str) -> None:
        """Run the full scaffolding pipeline."""
        self.project_root.mkdir(parents=True, exist_ok=True)
        skill_base = self.overlay_app.removeprefix("t3_").removesuffix("_overlay") or "overlay"
        skill_name = f"t3:{skill_base.replace('_', '-')}"
        skill_dir = self.project_root / "skills" / skill_name

        self.write_overlay(skill_name)
        self.write_skill_md(skill_dir, project_name, skill_name)
        self.copy_config_templates()
        self.write_pyproject(project_name)
