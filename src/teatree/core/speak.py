"""Local text-to-speech egress — the ``speak(text)`` seam (#1791).

A single chokepoint that reads the resolved :class:`~teatree.types.SpeakMode`
+ :class:`~teatree.types.SpeakTarget` and, when enabled, reads agent text
aloud. Two call sites drive it:

*   ``im-only`` / ``all`` — :func:`teatree.core.notify.notify_user` calls
    :func:`speak` for every bot→user IM/DM egress.
*   ``all`` — the Stop hook (``handle_speak_all_on_stop``) calls
    :func:`speak` with the transcript's last assistant text block.

The whole feature is gated on the macOS ``say`` binary being on ``PATH``
(:func:`binary_available`): when it is absent :func:`resolve_mode` forces
``off`` regardless of config, so the feature is simply inert off macOS — no
error, no nag. Cloud TTS (OpenAI / ElevenLabs) is a possible later backend
behind this same seam; out of scope here.

Delivery is governed by :class:`~teatree.types.SpeakTarget`:

*   ``local`` — ``say -o`` synthesises an AIFF, ``afconvert`` transcodes to
    ``.m4a``, and ``afplay`` plays it through the speakers. macOS-only; each
    step is independently no-op when its binary is absent.
*   ``slack-audio`` — the same ``.m4a`` is uploaded to the user's Slack DM
    via the messaging backend so the spoken reply reaches his phone.
*   ``both`` — both legs.

Every leg is non-blocking (spawned in a daemon thread / detached process)
and never raises into the caller's egress / Stop path — a failure to
synthesise or play is logged at debug and dropped.
"""

import logging
import re
import shutil
import tempfile
import threading
from pathlib import Path

from teatree.config import get_effective_settings
from teatree.types import SpeakMode, SpeakTarget
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail, run_checked

logger = logging.getLogger(__name__)

SAY_BINARY = "say"
_AFCONVERT_BINARY = "afconvert"

# Speech is throwaway and a long read is worse than no read — a capped
# excerpt keeps ``say`` from droning through a 4 KB status report.
_MAX_SPEAK_CHARS = 600

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_URL_RE = re.compile(r"https?://\S+")
_HEADING_BULLET_RE = re.compile(r"^\s*(?:#{1,6}|[-*+]|\d+\.)\s+", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"[*_~>|#]")
_WS_RE = re.compile(r"\s+")


def binary_available() -> bool:
    """Whether the ``say`` binary is on ``PATH`` (the feature prerequisite)."""
    return shutil.which(SAY_BINARY) is not None


def resolve_mode() -> SpeakMode:
    """The EFFECTIVE speak mode: configured value, forced ``off`` if ``say`` is absent.

    The single place the binary-presence gate is applied — both call
    sites resolve through here so the prerequisite check can never drift
    between the IM egress and the Stop hook.
    """
    if not binary_available():
        return SpeakMode.OFF
    return get_effective_settings().speak_mode


def clean_for_speech(text: str) -> str:
    """Strip markdown / code / URLs and cap length so ``say`` reads prose, not symbols.

    Code fences and inline code are dropped entirely (reading source aloud
    is noise); a ``[label](url)`` markdown link collapses to its label; a
    bare URL is dropped; heading/bullet/emphasis sigils are removed; runs
    of whitespace collapse to a single space. The result is truncated to
    :data:`_MAX_SPEAK_CHARS` on a word boundary with a trailing ``…``.
    """
    stripped = _CODE_FENCE_RE.sub(" ", text)
    stripped = _INLINE_CODE_RE.sub(" ", stripped)
    stripped = _MD_LINK_RE.sub(r"\1", stripped)
    stripped = _URL_RE.sub(" ", stripped)
    stripped = _HEADING_BULLET_RE.sub("", stripped)
    stripped = _EMPHASIS_RE.sub("", stripped)
    stripped = _WS_RE.sub(" ", stripped).strip()
    if len(stripped) <= _MAX_SPEAK_CHARS:
        return stripped
    head = stripped[:_MAX_SPEAK_CHARS].rsplit(" ", 1)[0].rstrip()
    return f"{head}…"


def speak(text: str, *, block: bool = False) -> None:
    """Read ``text`` aloud per the resolved mode + target — never raises.

    Resolves the effective :class:`SpeakMode` (forced ``off`` when ``say``
    is absent) and the configured :class:`SpeakTarget`, cleans the text
    for speech, and runs the enabled delivery legs. A blank cleaned text,
    ``off`` mode, or any delivery failure is a silent no-op.

    ``block=False`` (default) dispatches the delivery on a daemon thread so
    an in-process caller's egress path (``notify_user``) is never delayed.
    ``block=True`` runs delivery synchronously — used by the detached
    ``t3 speak`` subprocess the Stop hook spawns, whose whole job is to
    deliver before it exits (a daemon thread would die with the process).

    Callers gate on the mode SEMANTICS themselves (the IM egress speaks
    for ``im-only`` *and* ``all``; the Stop hook speaks only for ``all``)
    — this function just refuses ``off``.
    """
    if resolve_mode() is SpeakMode.OFF:
        return
    cleaned = clean_for_speech(text)
    if not cleaned:
        return
    target = get_effective_settings().speak_target
    if block:
        _deliver(cleaned, target)
        return
    thread = threading.Thread(target=_deliver, args=(cleaned, target), daemon=True)
    thread.start()


def _deliver(text: str, target: SpeakTarget) -> None:
    """Run the enabled delivery legs; contain every failure to a debug log.

    Runs on the daemon thread :func:`speak` spawns. The local and Slack
    legs are independent: a failure (or absence) of one never suppresses
    the other.
    """
    audio_path: Path | None = None
    try:
        if target.includes_local():
            _speak_local(text)
        if target.includes_slack():
            audio_path = _synthesise_m4a(text)
            if audio_path is not None:
                _upload_to_slack(audio_path)
    except Exception as exc:  # noqa: BLE001 — the speak seam must never raise into the caller
        logger.debug("speak delivery failed: %s", exc)
    finally:
        if audio_path is not None:
            shutil.rmtree(audio_path.parent, ignore_errors=True)


def _speak_local(text: str) -> None:
    """Play ``text`` through the macOS speakers via ``say`` — no-op if absent.

    Failure is tolerated (``run_allowed_to_fail`` with ``expected_codes=None``);
    a transport/timeout error is logged and dropped so the speak seam never
    raises into the caller.
    """
    say_bin = shutil.which(SAY_BINARY)
    if say_bin is None:
        return
    try:
        run_allowed_to_fail([say_bin, text], expected_codes=None, timeout=120)
    except (OSError, TimeoutExpired, CommandFailedError) as exc:
        logger.debug("local say failed: %s", exc)


def _synthesise_m4a(text: str) -> Path | None:
    """Synthesise ``text`` to a temp ``.m4a`` (``say -o`` AIFF → ``afconvert``).

    Returns the path on success, or ``None`` when a required binary is
    absent or a step fails — the Slack leg then silently no-ops. The
    caller owns deleting the returned file.
    """
    say_bin = shutil.which(SAY_BINARY)
    afconvert_bin = shutil.which(_AFCONVERT_BINARY)
    if say_bin is None or afconvert_bin is None:
        return None
    tmp_dir = Path(tempfile.mkdtemp(prefix="t3-speak-"))
    aiff_path = tmp_dir / "speech.aiff"
    m4a_path = tmp_dir / "speech.m4a"
    try:
        run_checked([say_bin, "-o", str(aiff_path), text], timeout=120)
        run_checked(
            [afconvert_bin, "-f", "m4af", "-d", "aac", str(aiff_path), str(m4a_path)],
            timeout=120,
        )
    except (OSError, TimeoutExpired, CommandFailedError) as exc:
        logger.debug("m4a synthesis failed: %s", exc)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None
    aiff_path.unlink(missing_ok=True)
    return m4a_path


def _upload_to_slack(audio_path: Path) -> None:
    """Upload the synthesised audio to the user's Slack DM — best-effort.

    Reuses the same backend + ``slack_user_id`` resolution the bot→user
    DM path uses (:func:`teatree.core.notify.notify_user`'s helpers), so
    a single config drives both. A non-ok upload body (e.g. ``files:write``
    scope missing) is logged at debug and dropped — the spoken reply just
    doesn't reach the phone this time.
    """
    from teatree.core.backend_factory import messaging_from_overlay  # noqa: PLC0415
    from teatree.core.notify import _resolve_user_id  # noqa: PLC0415

    backend = messaging_from_overlay()
    user_id = _resolve_user_id()
    if backend is None or not user_id:
        logger.debug("speak slack upload skipped: no backend or user_id")
        return
    channel = backend.open_dm(user_id)
    if not channel:
        logger.debug("speak slack upload skipped: open_dm returned empty channel")
        return
    body = backend.upload_audio_to_dm(channel=channel, filepath=str(audio_path), title="Agent reply")
    if not body.get("ok"):
        error = body.get("error", "no response")
        logger.debug("speak slack upload not ok: %s", error)
