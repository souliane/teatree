from importlib.resources import files
from pathlib import Path

import typer

app = typer.Typer(add_completion=False, no_args_is_help=True)


def main() -> None:
    app()


@app.callback()
def bootstrap() -> None:
    return


@app.command()
def startproject(
    project_name: str,
    destination: Path,
    *,
    overlay_app: str = typer.Option(..., "--overlay-app"),
    project_package: str | None = typer.Option(None, "--project-package"),
) -> None:
    project_root = destination / project_name
    if project_root.exists():
        msg = f"Destination already exists: {project_root}"
        typer.echo(msg)
        raise typer.Exit(code=1)

    package_name = project_package or project_name.replace("-", "_")
    project_root.mkdir(parents=True)
    src_dir = project_root / "src"
    src_dir.mkdir()
    project_package_dir = src_dir / package_name
    overlay_package = src_dir / overlay_app
    project_package_dir.mkdir()
    overlay_package.mkdir()

    skill_name = f"t3-{overlay_app.removesuffix('_overlay').replace('_', '-')}"
    skill_dir = project_root / "skills" / skill_name
    skill_dir.mkdir(parents=True)

    overlay_class_name = _camelize(overlay_app)
    context = {
        "overlay_app": overlay_app,
        "overlay_class_name": overlay_class_name,
        "project_name": project_name,
        "project_package": package_name,
        "skill_name": skill_name,
    }

    _write_template("project_template/manage.py-tpl", project_root / "manage.py", context)
    _write_template("project_template/package_init.py-tpl", project_package_dir / "__init__.py", context)
    _write_template("project_template/settings.py-tpl", project_package_dir / "settings.py", context)
    _write_template("project_template/urls.py-tpl", project_package_dir / "urls.py", context)
    _write_template("project_template/asgi.py-tpl", project_package_dir / "asgi.py", context)
    _write_template("project_template/wsgi.py-tpl", project_package_dir / "wsgi.py", context)

    _write_template("app_template/package_init.py-tpl", overlay_package / "__init__.py", context)
    _write_template("app_template/apps.py-tpl", overlay_package / "apps.py", context)
    _write_template("app_template/overlay.py-tpl", overlay_package / "overlay.py", context)
    _write_template("app_template/models.py-tpl", overlay_package / "models.py", context)
    _write_template("app_template/skill.md-tpl", skill_dir / "SKILL.md", context)

    typer.echo(str(project_root))


def _write_template(template_name: str, destination: Path, context: dict[str, str]) -> None:
    template = files("teetree.overlay_init").joinpath(template_name).read_text(encoding="utf-8")
    rendered = _render_template(template, context)
    destination.write_text(rendered, encoding="utf-8")


def _render_template(template: str, context: dict[str, str]) -> str:
    rendered = template
    for key, value in context.items():
        rendered = rendered.replace(f"{{{{ {key} }}}}", value)
    return rendered


def _camelize(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))
