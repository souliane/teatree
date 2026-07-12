"""``t3 admin`` — run the Django admin for the teatree project.

Teatree IS the Django project, so the admin binds to the canonical teatree
database (the same SQLite file every other ``t3`` command reads) — no overlay
or per-worktree DB context. ``core/admin.py`` registers the Ticket / Worktree /
Session / Task / TaskAttempt / PullRequest models, and ``urls.py`` mounts
``/admin/`` unconditionally (independent of ``DEBUG``).

The command makes the admin immediately usable from a cold checkout: it applies
migrations, collects static into ``STATIC_ROOT`` (so WhiteNoise serves the admin
and dashboard assets under gunicorn with DEBUG off), ensures a superuser exists
(creating one non-interactively from ``T3_ADMIN_USER`` / ``T3_ADMIN_PASSWORD``
when absent), opens the browser at ``/admin/``, then serves
``teatree.wsgi:application`` under gunicorn (a production WSGI server, not
Django's dev ``runserver``) in the foreground until interrupted. It is
DEBUG-agnostic — nothing here reads or sets ``DEBUG``.
"""

import threading
import webbrowser
from dataclasses import dataclass

import typer

from teatree.utils.django_bootstrap import ensure_django

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000
_DEFAULT_ADMIN_USER = "admin"
_GENERATED_PASSWORD_BYTES = 12
_BROWSER_OPEN_DELAY_SECONDS = 1.5
# One threaded worker, not N processes: the single-operator admin is I/O-light
# and the deploy caps it at 512 MB, where a second full-Django process risks OOM.
# Threads give concurrency (parallel asset requests, WAL concurrent reads) within
# one process's memory footprint.
_GUNICORN_WORKERS = 1
_GUNICORN_THREADS = 4
_GUNICORN_TIMEOUT_SECONDS = 120


@dataclass(frozen=True, slots=True)
class SuperuserResult:
    """The admin user the command resolved — with a password only when freshly created."""

    username: str
    created_password: str | None


def admin(
    *,
    host: str = typer.Option(_DEFAULT_HOST, "--host", help="Host interface for the admin gunicorn server."),
    port: int = typer.Option(_DEFAULT_PORT, "--port", help="Port for the admin gunicorn server."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open the browser at /admin/."),
) -> None:
    """Run the Django admin for the teatree project under a local gunicorn server."""
    ensure_django()

    _ensure_migrated()
    _collectstatic()
    superuser = _ensure_superuser()
    admin_url = f"http://{host}:{port}/admin/"

    typer.echo(f"teatree admin → {admin_url}")
    if superuser.created_password is not None:
        typer.echo(f"created superuser '{superuser.username}' with password '{superuser.created_password}'")
        typer.echo("set T3_ADMIN_USER / T3_ADMIN_PASSWORD to control these credentials")
    else:
        typer.echo(f"using existing superuser '{superuser.username}'")

    browser_timer = None if no_browser else _open_browser_when_ready(admin_url)

    try:
        _run_server(host, port)
    finally:
        if browser_timer is not None:
            browser_timer.join()


def _ensure_migrated() -> None:
    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    call_command("migrate", run_syncdb=True, verbosity=0)


def _collectstatic() -> None:
    """Populate ``STATIC_ROOT`` so WhiteNoise serves the dashboard assets under gunicorn.

    Runs on every admin boot (before gunicorn spawns and scans ``STATIC_ROOT``) so a
    cold checkout serves ``/static/`` with DEBUG off — without it the dashboard CSS
    and vendored JS 404 wholesale.
    """
    from django.core.management import call_command  # noqa: PLC0415 — deferred, post ensure_django()

    call_command("collectstatic", interactive=False, verbosity=0)


def _ensure_superuser() -> SuperuserResult:
    """Ensure a superuser exists, creating one non-interactively when absent.

    The password comes from ``T3_ADMIN_PASSWORD`` when set, otherwise a fresh
    random token is generated and surfaced to the caller — never a hardcoded
    default. An existing superuser is reused untouched (no password is exposed).
    """
    import os  # noqa: PLC0415 — deferred: loaded only when this command runs
    import secrets  # noqa: PLC0415 — deferred: loaded only when this command runs

    from django.contrib.auth import get_user_model  # noqa: PLC0415 — deferred: Django import at call time

    user_model = get_user_model()
    existing = user_model.objects.filter(is_superuser=True).first()
    if existing is not None:
        return SuperuserResult(username=existing.get_username(), created_password=None)

    username = os.environ.get("T3_ADMIN_USER") or _DEFAULT_ADMIN_USER
    password = os.environ.get("T3_ADMIN_PASSWORD") or secrets.token_urlsafe(_GENERATED_PASSWORD_BYTES)
    user_model.objects.create_superuser(username=username, password=password)
    return SuperuserResult(username=username, created_password=password)


def _open_browser_when_ready(url: str) -> threading.Timer:
    """Open the browser shortly after the server has had time to bind.

    Returns the started timer so the caller can join it once the server exits
    (and so the timer is not garbage-collected while still pending).
    """
    timer = threading.Timer(_BROWSER_OPEN_DELAY_SECONDS, webbrowser.open, args=(url,))
    timer.daemon = True
    timer.start()
    return timer


def _run_server(host: str, port: int) -> None:
    import sys  # noqa: PLC0415 — deferred: loaded only when this command runs

    from teatree.utils.run import CommandFailedError, run_streamed  # noqa: PLC0415 — deferred: keeps CLI startup light

    # gunicorn (a production WSGI server) against teatree's WSGI app — not
    # Django's dev ``runserver``, which is single-threaded, unfit for a
    # long-running process, and DEBUG-coupled. ``sys.executable -m gunicorn``
    # pins the tool-venv interpreter that has teatree + gunicorn on its path (a
    # bare ``gunicorn`` shim could resolve to a different environment with no
    # teatree, the same failure mode the old runserver invocation guarded).
    cmd = [
        sys.executable,
        "-m",
        "gunicorn",
        "teatree.wsgi:application",
        "--bind",
        f"{host}:{port}",
        "--workers",
        str(_GUNICORN_WORKERS),
        "--threads",
        str(_GUNICORN_THREADS),
        "--timeout",
        str(_GUNICORN_TIMEOUT_SECONDS),
    ]
    try:
        run_streamed(cmd)
    except KeyboardInterrupt:
        return
    except CommandFailedError as exc:
        raise SystemExit(exc.returncode) from exc
