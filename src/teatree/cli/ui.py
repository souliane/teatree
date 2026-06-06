"""``t3 ui`` — a trogon-backed terminal browser for the whole command tree.

Requires the optional ``ui`` dependency group (``uv sync --group ui``),
following the same lazy-import guard as ``docs`` in :mod:`teatree.cli.info`.
"""

import os

import typer


def ui() -> None:
    """Browse and run every t3 command in an interactive terminal UI.

    Requires the ``ui`` dependency group: ``uv sync --group ui``
    """
    try:
        from trogon.trogon import Trogon  # noqa: PLC0415
    except ImportError:
        typer.echo("ui browser requires the 'ui' extra: uv sync --group ui")
        raise typer.Exit(code=1) from None

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    import django  # noqa: PLC0415

    django.setup()

    from typer.main import get_group  # noqa: PLC0415

    from teatree.cli import app, register_overlay_commands  # noqa: PLC0415

    register_overlay_commands()
    Trogon(get_group(app), app_name="t3").run()
