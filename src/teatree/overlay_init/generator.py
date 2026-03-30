"""Legacy project scaffolder using Django startproject + monkey-patching.

The modern approach is in ``bootstrap.py`` which uses clean templates.
This module exists to support the ``t3 startproject`` CLI command until
``bootstrap.py`` fully replaces it.
"""

import subprocess  # noqa: S404
import sys
from pathlib import Path
from textwrap import dedent


def camelize(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


class ProjectScaffolder:
    """Generate a TeaTree overlay project from Django startproject + patches."""

    def __init__(self, project_root: Path, overlay_app: str, package_name: str) -> None:
        self.project_root = project_root
        self.overlay_app = overlay_app
        self.package_name = package_name
        self.overlay_class_name = camelize(overlay_app)
        self.src_dir = project_root / "src"

    def run_django_startproject(self) -> None:
        subprocess.run(  # noqa: S603
            [sys.executable, "-m", "django", "startproject", self.package_name, str(self.project_root)],
            check=True,
        )
        self.src_dir.mkdir()
        (self.project_root / self.package_name).rename(self.src_dir / self.package_name)

    def run_django_startapp(self) -> None:
        subprocess.run(  # noqa: S603
            [sys.executable, "-m", "django", "startapp", self.overlay_app],
            cwd=self.src_dir,
            check=True,
        )

    def patch_settings(self) -> None:
        settings_path = self.src_dir / self.package_name / "settings.py"
        text = settings_path.read_text(encoding="utf-8")
        extra_apps = [
            "django_htmx",
            "django_rich",
            "django_tasks",
            "django_tasks_db",
            "teatree.core",
            "teatree.agents",
            self.overlay_app,
        ]
        teatree_apps = "\n".join(f"    '{a}'," for a in extra_apps)
        text = text.replace(
            "'django.contrib.staticfiles',",
            f"'django.contrib.staticfiles',\n{teatree_apps}",
        )
        text += dedent(f"""

            # --- TeaTree ---
            TEATREE_OVERLAY_CLASS = "{self.overlay_app}.overlay.{self.overlay_class_name}Overlay"
            TEATREE_HEADLESS_RUNTIME = "claude-code"
            TEATREE_INTERACTIVE_RUNTIME = "codex"
            TEATREE_TERMINAL_MODE = "same-terminal"
            TEATREE_CLAUDE_STATUSLINE_STATE_DIR = "/tmp/claude-statusline"
            TEATREE_AGENT_HANDOVER = [
                {{
                    "runtime": "claude-code",
                    "telemetry": {{
                        "provider": "claude-statusline",
                        "switch_away_at_percent": 95,
                        "switch_back_at_percent": 80,
                    }},
                }},
                {{
                    "runtime": "codex",
                }},
            ]

            TASKS = {{
                "default": {{
                    "BACKEND": "django_tasks_db.DatabaseBackend",
                }},
            }}

            # Editable-install intent (verified by `t3 doctor check`).
            # Set to True when contributing to that package's source code.
            TEATREE_EDITABLE = False
            OVERLAY_EDITABLE = False
        """)
        settings_path.write_text(text, encoding="utf-8")

    def patch_urls(self) -> None:
        urls_path = self.src_dir / self.package_name / "urls.py"
        text = urls_path.read_text(encoding="utf-8")
        text = text.replace(
            "from django.urls import path",
            "from django.urls import include, path",
        )
        text = text.replace(
            "path('admin/', admin.site.urls),",
            "path('', include('teatree.core.urls')),\n    path('admin/', admin.site.urls),",
        )
        urls_path.write_text(text, encoding="utf-8")

    def patch_manage_py(self) -> None:
        manage_py = self.project_root / "manage.py"
        text = manage_py.read_text(encoding="utf-8")
        text = text.replace(
            "import sys\n",
            "import sys\nfrom pathlib import Path\n",
        )
        text = text.replace(
            "os.environ.setdefault",
            'sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))\n    os.environ.setdefault',
        )
        manage_py.write_text(text, encoding="utf-8")
        manage_py.chmod(0o755)

    def write_overlay(self, skill_name: str) -> None:
        overlay_path = self.src_dir / self.overlay_app / "overlay.py"
        overlay_path.write_text(
            dedent(f"""\
                from teatree.core.overlay import OverlayBase, ProvisionStep


                class {self.overlay_class_name}Overlay(OverlayBase):
                    def get_repos(self) -> list[str]:
                        return []

                    def get_provision_steps(self, worktree):
                        return []

                    def get_skill_metadata(self):
                        return {{"skill_path": "skills/{skill_name}/SKILL.md"}}
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
        }
        for source_name, dest_name in templates.items():
            source = template_dir.joinpath(source_name)
            dest = self.project_root / dest_name
            dest.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    def write_pyproject(self, project_name: str) -> None:
        from importlib.resources import files  # noqa: PLC0415

        template = (
            files("teatree.templates").joinpath("overlay").joinpath("pyproject.toml.tmpl").read_text(encoding="utf-8")
        )
        content = template.replace("{{project_name}}", project_name)
        content = content.replace("{{overlay_app}}", self.overlay_app)
        content = content.replace("{{package_name}}", self.package_name)
        content = content.replace("{{description}}", f"Generated TeaTree host project for {self.overlay_class_name}")
        self.project_root.joinpath("pyproject.toml").write_text(content, encoding="utf-8")

    def write_env(self) -> None:
        (self.project_root / ".env").write_text(
            f"DJANGO_SETTINGS_MODULE={self.package_name}.settings\n",
            encoding="utf-8",
        )

    def scaffold(self, project_name: str) -> None:
        """Run the full scaffolding pipeline."""
        skill_base = self.overlay_app.removeprefix("t3_").removesuffix("_overlay") or "overlay"
        skill_name = f"t3-{skill_base.replace('_', '-')}"
        skill_dir = self.project_root / "skills" / skill_name

        self.run_django_startproject()
        self.run_django_startapp()
        self.patch_settings()
        self.patch_urls()
        self.write_overlay(skill_name)
        self.write_skill_md(skill_dir, project_name, skill_name)
        self.copy_config_templates()
        self.write_pyproject(project_name)
        self.patch_manage_py()
        self.write_env()
