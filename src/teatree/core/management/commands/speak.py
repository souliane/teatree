"""``t3 speak`` — read the in-client turn aloud via the local speakers (#1791/#2050).

The detached worker the Stop hook spawns for ``scope = all`` (and a shell
entry point for ad-hoc use). It bootstraps Django (so the config resolves),
then calls :func:`teatree.core.speak.speak` with ``block=True`` so the local
read completes before the process exits — a non-blocking daemon thread would
die with the short-lived subprocess.

The local-speakers leg and the binary-presence gate live entirely in
:mod:`teatree.core.speak`: when ``local`` is off (the default, or forced off
because the ``say`` binary is absent) this is a clean no-op that exits 0. The
Slack-audio attach is owned by :func:`teatree.core.speak.deliver_user_dm` (it
rides each bot→user DM), not this command. ``--overlay`` sets
``T3_OVERLAY_NAME`` so the per-overlay ``[teatree.speak]`` config resolves.
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
        """Read ``text`` aloud synchronously through the local speakers per [teatree.speak]."""
        from teatree.core.speak import speak  # noqa: PLC0415

        body = sys.stdin.read() if text == "-" else text
        with _overlay_env(overlay):
            speak(body, block=True)
