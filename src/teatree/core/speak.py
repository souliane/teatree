"""Text-to-speech egress — the ``speak()`` seam + the shared user-DM chokepoint (#2060).

A single place that reads the resolved :class:`~teatree.types.SpeakConfig`
(a ``local`` :class:`~teatree.types.LocalPlayback` enum + a ``slack`` bool)
and delivers spoken agent text. Two distinct deliveries share one config:

*   :func:`deliver_user_dm` — the ONE chokepoint both bot→user DM egress
    points call (:func:`teatree.core.notify.notify_user` and the on-behalf
    self-DM in :func:`teatree.core.on_behalf_egress.OnBehalfSlackEgress.post`).
    When ``slack`` is on and synthesis succeeds it posts a SINGLE DM carrying
    the text + an inline audio attachment
    (:meth:`~teatree.backends.slack.bot.SlackBotBackend.post_audio_dm`); on a
    synthesis failure (or ``slack`` off) it degrades to a text-only
    :meth:`post_message`. Independently, when ``local`` is ``dm`` or ``all``
    the same text plays through the machine's speakers — so the user's own DM
    both reaches his phone with audio and reads aloud locally, driven from one
    call. The two axes are independent: Slack never auto-plays, so the local
    play is never suppressed by the Slack attach.
*   :func:`speak` — the in-client last-turn read the Stop hook drives via a
    detached ``t3 speak`` subprocess. It plays ONLY through the local speakers
    and only when ``local == all`` — in-client turns are never Slack messages,
    so there is no double-play to suppress.

The whole feature is gated on the macOS ``say`` binary being on ``PATH``
(:func:`binary_available`): when it is absent :func:`resolve_speak` forces the
feature inert, so it is simply silent off macOS — no error, no nag. The
``slack`` arm additionally needs ``afconvert`` (the macOS AIFF→m4a
transcoder) to build the audio attachment; when ``afconvert`` is absent
synthesis returns ``None`` and the DM degrades to text-only. A failed
``slack`` attach (``files:write`` scope missing) is surfaced once per error
class via a text DM (:func:`_surface_upload_failure`), so a missing scope
can't silently masquerade as working audio delivery.
"""

import logging
import re
import shutil
import tempfile
import threading
from pathlib import Path

from teatree.config import get_effective_settings
from teatree.core.backend_protocols import MessagingBackend
from teatree.types import RawAPIDict, SpeakConfig
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail, run_checked

logger = logging.getLogger(__name__)

SAY_BINARY = "say"
_AFCONVERT_BINARY = "afconvert"
_SPEAK_SUBPROCESS_TIMEOUT = 120

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


def resolve_speak() -> SpeakConfig:
    """The EFFECTIVE speak config: configured value, forced inert if ``say`` is absent.

    The single place the binary-presence gate is applied — every call site
    resolves through here so the prerequisite check can never drift between
    the DM egress, the on-behalf self-DM, and the Stop hook. The default
    :class:`SpeakConfig` is inert (``local = off``, ``slack = false``).
    """
    if not binary_available():
        return SpeakConfig()
    return get_effective_settings().speak


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
    """Read ``text`` aloud through the LOCAL speakers — never raises (#2060).

    The Stop-hook seam: the detached ``t3 speak`` subprocess calls this with
    the in-client turn's last assistant text. It plays only on the local
    speakers — the Slack-audio attach is owned by :func:`deliver_user_dm`,
    not this path — so a blank cleaned text, ``local`` not ``all``, or any
    failure is a silent no-op. The Stop-hook arm only spawns this when
    ``local == all``; in-client turns are never Slack messages, so the
    ``slack`` attach is irrelevant here.

    ``block=False`` dispatches on a daemon thread so an in-process caller is
    never delayed; ``block=True`` runs synchronously — used by the detached
    subprocess, whose whole job is to deliver before it exits.
    """
    config = resolve_speak()
    if not config.speaks_in_client_turns():
        return
    cleaned = clean_for_speech(text)
    if not cleaned:
        return
    if block:
        _speak_local(cleaned)
        return
    thread = threading.Thread(target=_speak_local, args=(cleaned,), daemon=True)
    thread.start()


def deliver_user_dm(
    backend: MessagingBackend,
    *,
    channel: str,
    text: str,
    thread_ts: str = "",
) -> RawAPIDict:
    """Post ONE bot→user DM, attaching spoken audio when ``slack`` is on (#2060).

    The single chokepoint both bot→user DM egress points call. ``text`` is
    the already-formatted DM body. When ``slack`` is on AND synthesis
    succeeds, posts a SINGLE message via :meth:`post_audio_dm` carrying
    ``text`` as the message + the audio inline; otherwise (``slack`` off,
    ``say``/``afconvert`` absent, synthesis or upload failure) degrades to a
    text-only :meth:`post_message` so the DM is never lost. Independently,
    when ``local`` is ``dm`` or ``all`` the same text plays through the
    speakers — never suppressed by ``slack`` (Slack never auto-plays).

    Returns the raw Slack body of whichever post ran so the caller finalises
    its delivery row exactly as a plain ``post_message`` would. Never lets a
    speak-side failure (config read, synthesis, attach, local play) drop the
    text DM: any such error degrades to a plain text-only post.
    """
    config = _resolve_speak_safe()
    audio_body = _maybe_post_with_audio(backend, config, channel=channel, text=text, thread_ts=thread_ts)
    if audio_body is not None:
        response = audio_body
    else:
        response = backend.post_message(channel=channel, text=text, thread_ts=thread_ts)
    _maybe_speak_local(config, text)
    return response


def _resolve_speak_safe() -> SpeakConfig:
    """Resolve the speak config, degrading to inert on any failure.

    The DM delivery must never be lost to a speak-config read error, so a
    failed :func:`resolve_speak` falls back to both-destinations-off (a plain
    text DM still goes out).
    """
    try:
        return resolve_speak()
    except Exception as exc:  # noqa: BLE001 — a config read must never drop the text DM
        logger.debug("speak config read failed; degrading to text-only DM: %s", exc)
        return SpeakConfig()


def _maybe_post_with_audio(
    backend: MessagingBackend,
    config: SpeakConfig,
    *,
    channel: str,
    text: str,
    thread_ts: str,
) -> RawAPIDict | None:
    """Post ``text`` + an inline audio attachment, or ``None`` to degrade to text-only.

    Returns the ``post_audio_dm`` body on a successful attach (the caller
    uses it as the delivery response), or ``None`` when ``slack`` is off,
    synthesis fails, or the attach returns a non-ok body — in every ``None``
    case the caller falls back to a text-only post so the DM is never dropped.
    A non-ok attach body additionally surfaces the failure once per error
    class so a missing ``files:write`` scope is visible.
    """
    if not config.slack:
        return None
    try:
        audio_path = synthesise(clean_for_speech(text))
        if audio_path is None:
            return None
        try:
            body = backend.post_audio_dm(channel=channel, filepath=str(audio_path), text=text, thread_ts=thread_ts)
        finally:
            shutil.rmtree(audio_path.parent, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 — a failed attach must degrade to a text DM, never drop it
        logger.debug("speak audio attach raised; degrading to text-only DM: %s", exc)
        return None
    if not body.get("ok"):
        error = str(body.get("error", "no response"))
        logger.debug("speak audio attach not ok: %s", error)
        _surface_upload_failure(error)
        return None
    return body


def _maybe_speak_local(config: SpeakConfig, text: str) -> None:
    """Play ``text`` on the local speakers when ``local`` plays DMs — best-effort.

    The local-speakers leg of a bot→user DM, independent of the Slack-audio
    attach (Slack never auto-plays): run on a daemon thread so the caller's
    egress path is never delayed, and contained so a synthesis/play failure
    never breaks the DM.
    """
    if not config.speaks_dms():
        return
    cleaned = clean_for_speech(text)
    if not cleaned:
        return
    thread = threading.Thread(target=_speak_local, args=(cleaned,), daemon=True)
    thread.start()


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
        run_allowed_to_fail([say_bin, text], expected_codes=None, timeout=_SPEAK_SUBPROCESS_TIMEOUT)
    except (OSError, TimeoutExpired, CommandFailedError) as exc:
        logger.debug("local say failed: %s", exc)


def synthesise(text: str) -> Path | None:
    """Synthesise ``text`` to a temp ``.m4a`` (``say -o`` AIFF → ``afconvert``).

    Returns the path on success, or ``None`` when a required binary is
    absent or a step fails — the caller then degrades to a text-only DM (or
    a silent no-op for the local leg). The caller owns deleting the returned
    file's parent directory.
    """
    say_bin = shutil.which(SAY_BINARY)
    afconvert_bin = shutil.which(_AFCONVERT_BINARY)
    if say_bin is None or afconvert_bin is None:
        return None
    tmp_dir = Path(tempfile.mkdtemp(prefix="t3-speak-"))
    aiff_path = tmp_dir / "speech.aiff"
    m4a_path = tmp_dir / "speech.m4a"
    try:
        run_checked([say_bin, "-o", str(aiff_path), text], timeout=_SPEAK_SUBPROCESS_TIMEOUT)
        run_checked(
            [afconvert_bin, "-f", "m4af", "-d", "aac", str(aiff_path), str(m4a_path)],
            timeout=_SPEAK_SUBPROCESS_TIMEOUT,
        )
    except (OSError, TimeoutExpired, CommandFailedError) as exc:
        logger.debug("m4a synthesis failed: %s", exc)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None
    aiff_path.unlink(missing_ok=True)
    return m4a_path


_MISSING_SCOPE_HINT = (
    "Re-run `t3 setup slack-bot` to reinstall the bot with the `files:write` "
    "scope it now declares, then the audio will reach your phone."
)


def _surface_upload_failure(error: str) -> None:
    """DM the user once per error class when the ``slack`` attach fails.

    A failed attach is otherwise invisible: the user enables ``slack``,
    hears nothing on his phone, and gets no signal why. The text DM still
    landed (the caller degraded), and the text DM path (``chat:write``) is
    healthy even when ``files:write`` is not, so this reaches the user.

    Idempotent per error class — the ``BotPing`` ledger dedupes the
    ``speak-upload-failed-<error>`` key. Never raises into the speak seam.
    """
    from teatree.core.notify import NotifyKind, notify_user  # noqa: PLC0415

    hint = _MISSING_SCOPE_HINT if error == "missing_scope" else ""
    message = f"Couldn't attach the spoken audio to your Slack DM (Slack error: {error})."
    if hint:
        message = f"{message} {hint}"
    try:
        notify_user(
            message,
            kind=NotifyKind.INFO,
            idempotency_key=f"speak-upload-failed-{error}",
        )
    except Exception as exc:  # noqa: BLE001 — surfacing must never break the speak seam
        logger.debug("speak upload-failure surface failed: %s", exc)
