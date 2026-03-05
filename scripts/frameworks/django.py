"""Django framework plugin for teatree.

Auto-detected when manage.py is found in a workspace repo.
Registered at 'framework' layer — overrides defaults, overridden by project.
"""

import os
import re
import subprocess
from pathlib import Path


def _detect_settings_module(ws: str) -> str:
    """Scan workspace repos for a manage.py and extract DJANGO_SETTINGS_MODULE."""
    with os.scandir(ws) as entries:
        for entry in entries:
            manage_py = Path(entry.path) / "manage.py"
            if not (entry.is_dir() and manage_py.is_file()):
                continue
            try:
                content = manage_py.read_text()
            except OSError:
                break
            match = re.search(r'DJANGO_SETTINGS_MODULE.*["\']([a-zA-Z_][\w.]+)["\']', content)
            return match.group(1) if match else ""
    return ""


def wt_env_extra(envfile: str) -> None:
    """Auto-detect DJANGO_SETTINGS_MODULE from manage.py and add to envfile."""
    from lib.env import workspace_dir

    ws = workspace_dir()

    # Find the first repo with manage.py to detect settings module
    settings_module = _detect_settings_module(ws)
    if settings_module:
        with Path(envfile).open("a", encoding="utf-8") as ef:
            ef.write(f"DJANGO_SETTINGS_MODULE={settings_module}\n")

    with Path(envfile).open("a", encoding="utf-8") as ef:
        ef.write("POSTGRES_DB=${WT_DB_NAME}\n")


def wt_post_db(project_dir: str) -> None:
    """Run Django migrations + create superuser."""
    print("  Running Django migrations...")
    subprocess.run(
        ["python", "manage.py", "migrate", "--no-input"],
        cwd=project_dir,
        check=False,
    )

    print("  Creating superuser...")
    env = os.environ.copy()
    env.setdefault(
        "DJANGO_SUPERUSER_EMAIL",
        os.environ.get("DJANGO_SUPERUSER_EMAIL", "developer@localhost"),
    )
    env.setdefault(
        "DJANGO_SUPERUSER_PASSWORD",
        os.environ.get("DJANGO_SUPERUSER_PASSWORD", "test"),
    )
    subprocess.run(
        ["python", "manage.py", "createsuperuser", "--noinput"],
        cwd=project_dir,
        env=env,
        capture_output=True,
        check=False,
    )


def wt_run_backend(*args: str) -> None:
    """Start Django dev server + Docker services."""
    from lib.env import resolve_context

    port = args[0] if args else os.environ.get("BACKEND_PORT", "8000")

    try:
        ctx = resolve_context()
        compose_file = Path(ctx.main_repo) / "docker-compose.yml"
        if compose_file.is_file():
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(compose_file),
                    "--project-directory",
                    ctx.main_repo,
                    "up",
                    "-d",
                    "--no-build",
                ],
                check=False,
            )
    except RuntimeError:
        pass

    print(f"Starting Django on 0.0.0.0:{port}...")
    subprocess.run(["python", "manage.py", "runserver", f"0.0.0.0:{port}"], check=False)


def wt_run_tests(*args: str) -> None:
    """Run tests with pytest (fallback: manage.py test)."""
    import shutil

    if shutil.which("pytest"):
        subprocess.run(["pytest", *args], check=False)
    else:
        subprocess.run(["python", "manage.py", "test", *args], check=False)


def register_django() -> None:
    from lib.registry import register

    register("wt_env_extra", wt_env_extra, "framework")
    register("wt_post_db", wt_post_db, "framework")
    register("wt_run_backend", wt_run_backend, "framework")
    register("wt_run_tests", wt_run_tests, "framework")
