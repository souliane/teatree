"""``t3 speak`` — read text aloud via the local text-to-speech seam (#1791).

The detached worker the Stop hook spawns for ``speak_mode = all`` and a
shell entry point for ad-hoc use. It bootstraps Django (so the config +
Slack backend resolve), then calls :func:`teatree.core.speak.speak` with
``block=True`` so synthesis + delivery complete before the process exits —
a non-blocking daemon thread would die with the short-lived subprocess.

Delivery and the binary-presence gate live entirely in
:mod:`teatree.core.speak`: when ``speak_mode`` resolves to ``off`` (the
default, or forced off because the ``say`` binary is absent) this is a
clean no-op that exits 0. ``--overlay`` sets ``T3_OVERLAY_NAME`` for the
call so the right per-overlay Slack credentials resolve for the
``slack-audio`` / ``both`` delivery targets.
"""

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

import typer
from django_typer.management import TyperCommand


@contextmanager
def _overlay_env(overlay: str) -> Iterator[None]:
    """Set ``T3_OVERLAY_NAME`` for the call, restoring the prior value after."""
    if not overlay:
        yield
        return
    previous = os.environ.get("T3_OVERLAY_NAME")
    os.environ["T3_OVERLAY_NAME"] = overlay
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("T3_OVERLAY_NAME", None)
        else:
            os.environ["T3_OVERLAY_NAME"] = previous


class Command(TyperCommand):
    def handle(
        self,
        text: Annotated[
            str,
            typer.Argument(help="Text to read aloud. Use ``-`` to read it from stdin."),
        ],
        overlay: Annotated[
            str,
            typer.Option("--overlay", help="Set T3_OVERLAY_NAME for the call (per-overlay Slack credentials)."),
        ] = "",
    ) -> None:
        """Read ``text`` aloud synchronously per the resolved speak mode + target."""
        from teatree.core.speak import speak  # noqa: PLC0415

        body = sys.stdin.read() if text == "-" else text
        with _overlay_env(overlay):
            speak(body, block=True)
