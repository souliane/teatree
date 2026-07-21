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

**Availability gate at playback, not at config.** The ``away`` state silences
LOCAL playback — not the ``slack`` arm, which still reaches the user's phone.
This gate belongs at the PLAYBACK call site (:func:`_speak_local`), not in
:func:`resolve_speak`. :func:`resolve_speak` returns the user's configured
:class:`~teatree.types.SpeakConfig` unchanged regardless of availability;
:func:`_speak_local` consults :func:`_is_away` and skips the ``say`` call when
away. The user's configured ``local`` is never mutated or overridden by
presence — only actual playback is gated.

**Cross-process speaker mutual exclusion (#2152).** Local playback fans out
from two independent sources — each DM's :func:`_maybe_speak_local` leg and
the detached ``t3 speak`` Stop-hook read — each spawning its own ``say``.
:func:`_speak_local` therefore takes a single machine-wide :func:`fcntl.flock`
on a lockfile under the teatree state dir (:func:`_speaker_lock_path`) around
the actual ``say`` call. The lock is best-effort: if the lockfile cannot be
opened, or the wait budget elapses before the lock is free, the read falls
through and plays anyway — a brief overlap is better than a silenced read, and
a lock error must never mute audio.

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

import fcntl
import logging
import shutil
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from teatree.config import get_effective_settings
from teatree.core import presence
from teatree.core.backend_protocols import MessagingBackend
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.speak_cleaning import clean_for_speech
from teatree.paths import get_data_dir
from teatree.types import RawAPIDict, SpeakConfig
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail, run_checked

logger = logging.getLogger(__name__)

__all__ = [
    "clean_for_speech",
    "deliver_user_dm",
    "deliver_user_dm_sidecar",
    "resolve_speak",
    "speak",
    "synthesise",
]

SAY_BINARY = "say"
_AFCONVERT_BINARY = "afconvert"
_SPEAK_SUBPROCESS_TIMEOUT = 120

# A single machine-wide lockfile gives every ``say`` invocation MUTUAL
# EXCLUSION — across in-process daemon threads AND the separate detached
# ``t3 speak`` subprocesses — so local reads never talk over each other.
_SPEAKER_LOCK_NAMESPACE = "speak"
_SPEAKER_LOCK_FILENAME = "speaker.lock"

# Bounded wait for the speaker lock (#2156). The lock is acquired NON-BLOCKING
# in a short retry loop with a total budget: a read that cannot acquire it
# within the budget falls through and plays without serialization rather than
# being dropped. Local reads fan out from many sources (every bot→user DM
# local-play leg + every Stop-hook ``t3 speak`` read), so an unbounded blocking
# acquire builds a multi-minute backlog under a flood — a message could play
# 15 min after it was printed. A brief overlap is strictly better than a
# 15-min-late read: serialization holds in the common case and latency is capped
# at the budget. The budget is short relative to a single read (a ``say`` of the
# capped excerpt is well under a second), so a read only falls through when the
# speaker is genuinely saturated.
_SPEAKER_LOCK_WAIT_BUDGET_S = 2.0
_SPEAKER_LOCK_RETRY_INTERVAL_S = 0.05


def binary_available() -> bool:
    """Whether the ``say`` binary is on ``PATH`` (the feature prerequisite)."""
    return shutil.which(SAY_BINARY) is not None


def resolve_speak() -> SpeakConfig:
    """The user's configured speak settings — binary-presence gate only.

    Returns the effective user config when the ``say`` binary is present,
    otherwise an inert :class:`SpeakConfig` (``local = off``,
    ``slack = false``). The away gate is NOT applied here: availability
    affects PLAYBACK, not the config value. Call sites that drive local audio
    (:func:`_speak_local`, :func:`_maybe_speak_local`) consult
    :func:`_is_away` themselves so the user's configured ``local`` is always
    preserved and availability never mutates it.
    """
    if not binary_available():
        return SpeakConfig()
    return get_effective_settings().speak


def _is_away() -> bool:
    """Whether availability puts the user away — silences local TTS; never raises.

    True for holiday-``away`` AND ``autonomous_away`` (#2544): both mean the
    user is not at the keyboard, so playing to an empty room is pointless. Only
    LOCAL playback is gated — the Slack arm still reaches the user's phone.

    A resolution failure degrades to NOT away (returns ``False``): the
    away-gate must never suppress local audio on a transient error.
    """
    from teatree.loop.mode_resolution import resolve_active_mode

    try:
        return resolve_active_mode().defers_questions
    except Exception as exc:  # noqa: BLE001 — a resolution failure must never mute local audio
        logger.debug("mode resolution failed; treating as present: %s", exc)
        return False


def _in_meeting() -> bool:
    """Whether a configured presence backend reports the user in a meeting (#2171).

    Only a positive ``IN_MEETING`` mutes local playback; every other verdict
    (incl. any error) fails safe to audible. The Slack-audio arm is not gated.
    Runs on the local-TTS daemon threads, so the presence resolution reads
    config Django-free (see :func:`presence._effective_speak`) — no ORM
    connection is opened on the worker thread.
    """
    try:
        return presence.current_presence() is presence.Presence.IN_MEETING
    except Exception as exc:  # noqa: BLE001 — a presence failure must never mute local audio
        logger.debug("presence resolution failed; treating as not-in-meeting: %s", exc)
        return False


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

    The single chokepoint the ``notify post`` command calls for its direct
    channel-post path (not the ``notify_user`` bot→user path — see
    :func:`deliver_user_dm_sidecar` for that). ``text`` is the DM body.
    When ``slack`` is on AND synthesis succeeds, posts a SINGLE message via
    :meth:`post_audio_dm` carrying ``text`` as the message + the audio
    inline; otherwise (``slack`` off, ``say``/``afconvert`` absent, synthesis
    or upload failure) degrades to a text-only :meth:`post_message` so the
    DM is never lost. Independently, when ``local`` is ``dm`` or ``all`` the
    same text plays through the speakers — never suppressed by ``slack``
    (Slack never auto-plays).

    Returns the raw Slack body of whichever post ran so the caller finalises
    its delivery row exactly as a plain ``post_message`` would. Never lets a
    speak-side failure (config read, synthesis, attach, local play) drop the
    text DM: any such error degrades to a plain text-only post.

    Pure delivery: it does NOT interpret ``thread_ts`` as an answer. Retiring
    a queued question is owned by the deliberate threaded-answer egress (the
    self-DM branch of
    :meth:`teatree.core.on_behalf_egress.OnBehalfSlackEgress.post`), so an
    unrelated INFO/status DM that ``notify_user`` happens to thread under a
    still-open question can never retire it.
    """
    config = _resolve_speak_safe()
    audio_body = (
        _maybe_post_with_audio(backend, channel=channel, text=text, thread_ts=thread_ts) if config.slack else None
    )
    if audio_body is not None:
        response = audio_body
    else:
        response = backend.post_message(channel=channel, text=text, thread_ts=thread_ts)
    _maybe_speak_local(config, text)
    return response


def deliver_user_dm_sidecar(
    backend: MessagingBackend,
    *,
    channel: str,
    text: str,
    thread_ts: str = "",
    initial_comment: str | None = None,
) -> None:
    """Run the speak side-effects for an already-delivered bot→user DM — never raises (#2054).

    Called AFTER the canonical text delivery has already landed and its ``ts``
    has been captured. This function owns the two enrichment arms that do NOT
    produce the delivery ``ts``:

    *   **Slack audio attachment** — when ``slack`` is on, attach a spoken
        audio file to the DM via ``post_audio_dm``. The attachment is a
        pure enhancement; if it fails the text DM already exists. The
        ``post_audio_dm`` response is intentionally ignored here: the caller
        already has the ``ts`` from the text delivery and does not need it.
    *   **Local playback** — play the text through the machine's speakers
        when ``local`` is ``dm`` or ``all``.

    ``initial_comment`` controls the text posted ALONGSIDE the audio (F4.4).
    ``None`` (the default) reuses ``text`` as the audio DM's ``initial_comment``
    — the ``t3 speak-dm`` path, whose audio DM stands on its own. The
    :func:`teatree.core.notify._deliver_dm` caller passes ``""`` together with
    ``thread_ts`` set to the delivered message's ``ts``, so the audio threads
    UNDER the text DM that already landed with NO repeated text — the text is
    delivered exactly once. In every case the audio is still synthesised from
    the full ``text``.

    Both arms are best-effort: any exception is swallowed so a speak failure
    can NEVER retroactively break a text DM that has already landed. The
    caller also guards this call, so returning ``None`` without raising is the
    cleaner contract.
    """
    config = _resolve_speak_safe()
    if config.slack:
        try:
            _maybe_post_with_audio(
                backend, channel=channel, text=text, thread_ts=thread_ts, initial_comment=initial_comment
            )
        except Exception as exc:  # noqa: BLE001 — sidecar must never break the delivered text DM
            logger.debug("deliver_user_dm_sidecar audio attach failed: %s", exc)
    _maybe_speak_local(config, text)


def _resolve_speak_safe() -> SpeakConfig:
    """Resolve the speak config, degrading to inert on a speak read failure.

    The DM delivery must never be lost to a speak-config read error, so a failed
    :func:`resolve_speak` falls back to both-destinations-off (a plain text DM
    still goes out). The degradation is loudness-graded by failure class (#258).

    A :class:`ValueError` is config CORRUPTION — a stored ``ConfigSetting`` row
    that fails its registry parser raises ``ValueError`` from
    ``get_effective_settings`` by design (the loud-failure intent). It is logged
    at ERROR (via ``logger.exception``, with the traceback) so the corruption is
    visible, never swallowed at debug (which would undo the loud-failure intent).

    Any other failure (a transient probe error, an unconfigured Django on the
    bootstrap path) is a genuinely-optional speak read and stays a quiet debug
    degradation — TTS is best-effort, the text DM is what must survive.

    Either way the text DM still goes out via the inert :class:`SpeakConfig`.
    """
    try:
        return resolve_speak()
    except ValueError:
        logger.exception("speak config read failed on a corrupt config value; degrading to text-only DM")
        return SpeakConfig()
    except Exception as exc:  # noqa: BLE001 — a config read must never drop the text DM
        logger.debug("speak config read failed; degrading to text-only DM: %s", exc)
        return SpeakConfig()


def _maybe_post_with_audio(
    backend: MessagingBackend,
    *,
    channel: str,
    text: str,
    thread_ts: str,
    initial_comment: str | None = None,
) -> RawAPIDict | None:
    """Post an inline audio attachment (with ``text``), or ``None`` to degrade to text-only.

    Callers gate this on ``config.slack`` — it is only reached when the Slack
    audio arm is on. Returns the ``post_audio_dm`` body on a successful attach
    (the caller uses it as the delivery response), or ``None`` when synthesis
    fails or the attach returns a non-ok body — in every ``None`` case the
    caller falls back to a text-only post so the DM is never dropped. A non-ok
    attach body additionally surfaces the failure once per error class so a
    missing ``files:write`` scope is visible.

    The audio is always synthesised from the full ``text``. ``initial_comment``
    is the text posted ALONGSIDE the audio: ``None`` (default) reuses ``text``
    (the single-message :func:`deliver_user_dm` path), while an explicit ``""``
    posts the audio with no comment — used when the text has already been
    delivered separately and the audio only threads under it (F4.4), so the
    body is never sent twice.
    """
    comment = text if initial_comment is None else initial_comment
    try:
        audio_path = synthesise(clean_for_speech(text))
        if audio_path is None:
            return None
        try:
            body = backend.post_audio_dm(channel=channel, filepath=str(audio_path), text=comment, thread_ts=thread_ts)
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
    never breaks the DM. The away gate is applied inside :func:`_speak_local`
    at actual playback time.
    """
    if not config.speaks_dms():
        return
    cleaned = clean_for_speech(text)
    if not cleaned:
        return
    thread = threading.Thread(target=_speak_local, args=(cleaned,), daemon=True)
    thread.start()


def _speaker_lock_path() -> Path:
    """The single machine-wide lockfile that serializes every ``say`` (#2152).

    Lives under the canonical teatree state dir
    (:func:`teatree.paths.get_data_dir`), never an ad-hoc path, so the in-process
    daemon threads and the separate detached ``t3 speak`` subprocesses all flock
    the SAME file — the only way to serialize across processes.
    """
    return get_data_dir(_SPEAKER_LOCK_NAMESPACE) / _SPEAKER_LOCK_FILENAME


@contextmanager
def _serial_speaker() -> Iterator[bool]:
    """Hold the cross-process speaker lock for one ``say`` if it can — best-effort (#2152).

    A context manager whose body ALWAYS plays: the yielded bool never gates the
    read (the caller plays regardless), it only reports whether serialization
    was achieved so the caller can log the unserialized case. The value is:

    *   ``True`` — either the exclusive lock is HELD for the ``with`` body
        (serialized, no overlap), OR the lockfile could not be opened at all
        (no state dir / permissions) so serialization is impossible. Both are
        the "nothing more to wait on, just play" case, so they share ``True``;
        the lockfile-open failure is already noted at debug on its own.
    *   ``False`` — the lockfile opened but the wait budget
        (:data:`_SPEAKER_LOCK_WAIT_BUDGET_S`) elapsed before the lock came free
        (the speaker is genuinely saturated). The read still plays, unserialized;
        ``False`` lets the caller log that it fell through.

    So a brief overlap is possible whenever the lock is not held — strictly
    better than a silenced or minutes-late read. The bounded wait keeps the
    daemon thread from blocking indefinitely, and this runs INSIDE the daemon
    thread, never on the caller's egress path, so it never delays a DM or turn.
    """
    try:
        lock_path = _speaker_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fh = lock_path.open("a", encoding="utf-8")
    except OSError as exc:
        logger.debug("speaker lock unavailable; playing without serialization: %s", exc)
        yield True
        return
    try:
        if not _acquire_within_budget(lock_fh):
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
    finally:
        lock_fh.close()


def _acquire_within_budget(lock_fh: IO[str]) -> bool:
    """Non-blocking ``flock`` retried until acquired or the wait budget elapses.

    Returns ``True`` once the exclusive lock is held, ``False`` if the budget
    (:data:`_SPEAKER_LOCK_WAIT_BUDGET_S`) elapses first. A short
    :data:`_SPEAKER_LOCK_RETRY_INTERVAL_S` sleep between tries keeps the busy-wait
    cheap. The first try happens before any sleep, so an uncontended lock is
    acquired immediately.
    """
    deadline = time.monotonic() + _SPEAKER_LOCK_WAIT_BUDGET_S
    while True:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(_SPEAKER_LOCK_RETRY_INTERVAL_S)
        else:
            return True


def _speak_local(text: str) -> None:
    """Play ``text`` through the macOS speakers via ``say`` — no-op if absent, away, or in a meeting.

    Applies the away AND meeting gates at the playback call site (#2171): a
    presence-reported meeting mutes local playback exactly as ``away`` does,
    with the same Slack-arm exemption. Availability stays out of config
    resolution — :func:`resolve_speak` returns the user's configured value
    unchanged; only playback is gated.

    Mutually exclusive machine-wide: the actual ``say`` call runs under the
    cross-process :func:`_serial_speaker` lock so concurrent local reads (in
    other threads or separate ``t3 speak`` subprocesses) never overlap. The lock
    is best-effort: if it cannot be acquired within the wait budget, the read
    falls through and plays anyway (without serialization) — a concurrent overlap
    is better than a silenced read. Failure of ``say`` itself is tolerated
    (``run_allowed_to_fail`` with ``expected_codes=None``); a transport/timeout
    error is logged and dropped so the speak seam never raises into the caller.
    """
    if _is_away() or _in_meeting():
        return
    say_bin = shutil.which(SAY_BINARY)
    if say_bin is None:
        return
    try:
        with _serial_speaker() as may_play:
            if not may_play:
                logger.debug("speaker busy; playing without serialization")
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
    from teatree.core.notify import NotifyKind, notify_user  # noqa: PLC0415 — deferred: call-time import, kept lazy

    hint = _MISSING_SCOPE_HINT if error == "missing_scope" else ""
    message = f"Couldn't attach the spoken audio to your Slack DM (Slack error: {error})."
    if hint:
        message = f"{message} {hint}"
    try:
        notify_user(
            message,
            kind=NotifyKind.INFO,
            idempotency_key=f"speak-upload-failed-{error}",
            audience=NotifyAudience.OWNER_ESCALATION,
        )
    except Exception as exc:  # noqa: BLE001 — surfacing must never break the speak seam
        logger.debug("speak upload-failure surface failed: %s", exc)
