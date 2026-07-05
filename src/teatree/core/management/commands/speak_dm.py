"""``t3 speak-dm`` — attach spoken audio to an already-posted user DM (#2171).

The detached worker the question-mirror hook spawns so the AskUserQuestion Slack
DM reaches the user's phone with audio, exactly like a ``notify_user`` DM does.
The hook already posted the question TEXT (and captured its ``ts`` for the #1174
reply matcher); this command runs the speak SIDE-EFFECTS — resolve the overlay's
Slack backend and call :func:`teatree.core.speak.deliver_user_dm_sidecar`
(``speak.slack`` → an audio attachment; ``speak.local`` → local playback, itself
gated by the away/meeting mute). It is dispatched detached (``start_new_session``)
so synthesis + upload never blocks the ~5 s hook budget, mirroring the detached
``t3 speak`` Stop-hook read.

Best-effort by contract: no backend, an unresolvable channel, or a speak
failure is a clean no-op — the text question already landed. ``--overlay`` sets
``T3_OVERLAY_NAME`` so the per-overlay Slack credentials resolve; ``--text -``
reads the body from stdin.
"""

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

import typer
from django_typer.management import TyperCommand

from teatree.core.backend_factory import messaging_from_overlay
from teatree.core.speak import deliver_user_dm_sidecar


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
        channel: Annotated[str, typer.Argument(help="Slack DM channel id the audio attaches to.")],
        text: Annotated[str, typer.Argument(help="Text to speak. Use ``-`` to read it from stdin.")],
        thread_ts: Annotated[str, typer.Option("--thread-ts", help="Thread the audio DM under this ts.")] = "",
        overlay: Annotated[
            str, typer.Option("--overlay", help="Set T3_OVERLAY_NAME for the call (per-overlay Slack credentials).")
        ] = "",
    ) -> None:
        """Attach spoken audio to the DM at ``channel`` per [teatree.speak] (no-op unless slack/local on)."""
        body = sys.stdin.read() if text == "-" else text
        if not channel or not body.strip():
            return
        with _overlay_env(overlay):
            backend = messaging_from_overlay(overlay or None)
            if backend is None:
                return
            deliver_user_dm_sidecar(backend, channel=channel, text=body, thread_ts=thread_ts)
