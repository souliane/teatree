"""Router-side wiring the ``teatree.hooks.slack_mirror`` leaf is injected with.

A bare ``hooks/scripts`` sibling of ``hook_router`` (the router is shrink-only,
module-health-capped). The mirror leaf is a pure ``teatree.hooks`` platform leaf
that imports only stdlib; the concrete higher-layer capabilities it needs are
INJECTED. This module builds them:

*   ``slack_http_poster`` — the hardened ``SlackHttpClient.post`` (#1110), built
    with the router's short per-call timeout and NO retry so the synchronous
    mirror never blows the hook budget.
*   ``dispatch_dm_audio`` / ``build_dm_audio_enricher`` — the #2171 audio
    enrichment: after the text question DM lands, ``t3 speak-dm`` is spawned
    DETACHED (the same pattern as the Stop-hook ``t3 speak`` read) so synthesis
    never blocks the mirror.

Cold-import safe: stdlib plus the stdlib-only ``t3_invocation`` seam at module
top; the ``teatree.backends.slack`` edge is a deferred import inside
``slack_http_poster``.
"""

import contextlib
import os
import shutil
from collections.abc import Callable

from hooks.scripts.t3_invocation import spawn_t3_detached, t3_argv, t3_available

# The mirror runs synchronously inside the hook timeout, so the client carries a
# short per-call timeout and NO retry (a retry-with-backoff could blow the budget).
_SLACK_POST_TIMEOUT_SECONDS = 2.0

type AudioEnricher = Callable[[str, str, str], None]
# The Slack ``post`` capability the leaf's ``Poster`` protocol expects; typed
# structurally here so this module needs no import of the higher-layer client.
type SlackPoster = Callable[..., dict]


def slack_http_poster() -> SlackPoster:
    """Build the hook-budget Slack poster: ``SlackHttpClient.post``, no retry.

    The router's platform→domain edge (the router is tach-invisible), injected
    into the pure ``slack_mirror`` leaf.
    """
    from teatree.backends.slack.http import SlackHttpClient  # noqa: I001,PLC0415 -- deferred domain edge; module stays cold-import safe

    return SlackHttpClient(timeout=_SLACK_POST_TIMEOUT_SECONDS, max_retries=0).post


def dispatch_dm_audio(channel: str, text: str, thread_ts: str) -> None:
    """Spawn ``t3 speak-dm`` detached to attach audio to a delivered DM — best-effort."""
    argv = t3_argv("speak-dm", "--channel", channel, "--text", text)
    if argv is None:
        return
    if thread_ts:
        argv.extend(["--thread-ts", thread_ts])
    overlay = os.environ.get("T3_OVERLAY_NAME", "")
    if overlay:
        argv.extend(["--overlay", overlay])
    with contextlib.suppress(Exception):
        spawn_t3_detached(argv)


def build_dm_audio_enricher(*, slack_enabled: bool) -> AudioEnricher | None:
    """The audio enricher for the question mirror, or ``None`` when audio is off.

    ``None`` (today's text-only mirror) unless ``speak.slack`` is on AND the
    ``say`` + ``t3`` binaries are present — synthesis needs ``say``/``afconvert``
    and the detached worker needs ``t3``. When enabled it returns the
    detached-dispatch callable the leaf fires (once) after the text question
    lands, so BOTH question surfaces (present-mode mirror and the away-mode
    ``DeferredQuestion`` capture) carry audio to the user's phone.
    """
    if not slack_enabled:
        return None
    if shutil.which("say") is None or not t3_available():
        return None
    return dispatch_dm_audio
