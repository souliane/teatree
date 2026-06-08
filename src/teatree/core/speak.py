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
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from teatree.config import get_effective_settings
from teatree.core.backend_protocols import MessagingBackend
from teatree.paths import get_data_dir
from teatree.types import RawAPIDict, SpeakConfig
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail, run_checked

logger = logging.getLogger(__name__)

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

# Slack emoji shortcodes (``:information_source:``, ``:white_check_mark:``) are
# status decorations, not prose. ``say`` voices them letter-soup
# (``:information_source:`` reads as "information source"), so they are dropped
# wholesale so they never reach the speakers as gibberish.
_EMOJI_SHORTCODE_RE = re.compile(r":[a-z0-9_+-]+:", re.IGNORECASE)

# A leading emoji shortcode is a status decoration — a line that STARTS with one
# is a status line (the notify ``:information_source: *info*`` prefix, a
# ``:white_check_mark: test green`` check). Real prose never opens a line with a
# ``:shortcode:``.
_LEADING_EMOJI_RE = re.compile(r"^\s*(?::[a-z0-9_+-]+:\s*)+", re.IGNORECASE)

# After markdown stripping, the notify kind marker (``*info*`` -> ``info``) is a
# bare status word. These three are the only notify kinds (``NotifyKind``), so a
# line that is ONLY one of them is the marker preamble, never a real message.
_NOISE_KIND_MARKERS = frozenset({"info", "answer", "question"})

# A log line leads with a level token AND a genuine log discriminator —
# ``INFO: ...``, ``[DEBUG] ...``, ``WARNING - ...``, ``Notice: ...``. Two shapes:
# a BRACKETED level (``[DEBUG]`` — the closing bracket is the discriminator), or
# a bare level immediately followed by a ``:`` or ``-`` separator. The
# discriminator is REQUIRED: without it the leading word is ordinary prose that
# merely happens to start with a level token ("Warning users now about the
# outage", "Critical bug found in prod.") and must stay spoken. Anchored to the
# line START so the same words mid-sentence ("the info you asked for") stay prose.
_LOG_LEVEL_LINE_RE = re.compile(
    # bracketed level (``[DEBUG]`` — closing bracket is the discriminator) ...
    r"^(?:\[\s*(?:trace|debug|info|notice|warn|warning|error|critical|fatal)\s*\]\s*[:\-]?\s+\S"
    # ... or a bare level immediately followed by a required ``:`` / ``-`` separator.
    r"|(?:trace|debug|info|notice|warn|warning|error|critical|fatal)\s*[:\-]\s+\S)",
    re.IGNORECASE,
)

# Sentence-ending punctuation. A terse status fragment ("test green") has none;
# a real emoji-led message ("We shipped the release!") does, so it is kept.
_SENTENCE_END_RE = re.compile(r"[.!?]")


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
    """Whether availability resolves to ``away`` — never raises.

    A resolution failure degrades to NOT away (returns ``False``): the
    away-gate must never suppress local audio on a transient error.
    """
    from teatree.core import availability  # noqa: PLC0415

    try:
        return availability.resolve_mode().mode == availability.MODE_AWAY
    except Exception as exc:  # noqa: BLE001 — a resolution failure must never mute local audio
        logger.debug("availability resolution failed; treating as present: %s", exc)
        return False


def clean_for_speech(text: str) -> str:
    """Strip markdown / code / URLs / status noise and cap length so ``say`` reads prose.

    Code fences and inline code are dropped entirely (reading source aloud
    is noise); a ``[label](url)`` markdown link collapses to its label; a
    bare URL is dropped; Slack emoji shortcodes (``:information_source:``) are
    dropped; heading/bullet/emphasis sigils are removed. Whole LOG/STATUS lines
    are then filtered out (#277): a line that is only a notify kind marker
    (``*info*``/``*answer*``/``*question*``) or that leads with a log level
    (``INFO:``/``[DEBUG]``) is droning preamble — ``say`` reads it as
    "Info source", "Info test green" before the real message — so it is
    dropped, while a real message that merely CONTAINS those words inline is
    kept. Surviving lines are joined, runs of whitespace collapse to a single
    space, and the result is truncated to :data:`_MAX_SPEAK_CHARS` on a word
    boundary with a trailing ``…``.
    """
    stripped = _CODE_FENCE_RE.sub(" ", text)
    stripped = _INLINE_CODE_RE.sub(" ", stripped)
    stripped = _MD_LINK_RE.sub(r"\1", stripped)
    stripped = _URL_RE.sub(" ", stripped)
    stripped = _HEADING_BULLET_RE.sub("", stripped)
    # Filter whole status/log lines BEFORE the emoji strip, so a leading emoji
    # shortcode is still visible to the line discriminator.
    kept = [line for line in stripped.splitlines() if not _is_noise_line(line)]
    stripped = "\n".join(kept)
    stripped = _EMOJI_SHORTCODE_RE.sub(" ", stripped)
    stripped = _EMPHASIS_RE.sub("", stripped)
    stripped = _WS_RE.sub(" ", stripped).strip()
    if len(stripped) <= _MAX_SPEAK_CHARS:
        return stripped
    head = stripped[:_MAX_SPEAK_CHARS].rsplit(" ", 1)[0].rstrip()
    return f"{head}…"


def _is_noise_line(line: str) -> bool:
    """Whether ``line`` is a log/status marker rather than user-facing prose (#277).

    Run BEFORE the emoji strip (so a leading ``:shortcode:`` is still visible).
    A line is status/log noise in any of three cases:

    *   **Notify kind marker** — once its emphasis sigils are gone the line is
        only ``info``/``answer``/``question`` (the three ``NotifyKind`` values),
        the DM preamble :func:`teatree.core.notify._format` prepends.
    *   **Log level line** — it leads with a level token (``INFO:``, ``[DEBUG]``,
        ``WARNING -``) as a distinct leading token. The same words mid-sentence
        ("the info you asked for") are prose and survive — the level must LEAD.
    *   **Emoji-led terse status** — it opens with a ``:shortcode:`` AND, with the
        emoji and sigils removed, carries no sentence-ending punctuation
        (``:white_check_mark: test green``). A real emoji-led message ends a
        sentence ("We shipped the release!") and is kept.

    Blank lines are never noise (they collapse away in the whitespace pass).
    """
    candidate = line.strip()
    if not candidate:
        return False
    bare = _EMPHASIS_RE.sub("", _EMOJI_SHORTCODE_RE.sub(" ", candidate)).strip()
    if bare.lower() in _NOISE_KIND_MARKERS:
        return True
    if _LOG_LEVEL_LINE_RE.match(bare):
        return True
    led_with_emoji = _LEADING_EMOJI_RE.match(candidate) is not None
    return led_with_emoji and not _SENTENCE_END_RE.search(bare)


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

    Pure delivery: it does NOT interpret ``thread_ts`` as an answer. Retiring
    a queued question is owned by the deliberate threaded-answer egress (the
    self-DM branch of
    :meth:`teatree.core.on_behalf_egress.OnBehalfSlackEgress.post`), so an
    unrelated INFO/status DM that ``notify_user`` happens to thread under a
    still-open question can never retire it.
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
    """Try to hold the cross-process speaker lock for one ``say`` — best-effort (#2152).

    Yields ``True`` always — either with the lock held (serialized, no
    overlap), or without it (fail-open: lock unavailable or budget elapsed).
    Yields ``False`` to signal to the caller that serialization was skipped so
    it can log accordingly; ``False`` does NOT mean the read is dropped.

    The lock prevents two ``say`` calls from overlapping when acquired within
    the budget. If the budget elapses (speaker still busy) the read falls
    through and plays anyway — a brief overlap is better than a silenced read,
    and the budget keeps the wait from blocking the daemon thread indefinitely.

    Best-effort: if the lockfile cannot be opened (no state dir, permissions)
    the read still plays — a lock error must never mute audio. Runs INSIDE the
    daemon thread, never on the caller's egress path, so the bounded wait
    never delays a DM or turn.
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
    """Play ``text`` through the macOS speakers via ``say`` — no-op if absent or away.

    Applies the away gate at the playback call site: if the user is currently
    away, the read is skipped silently. This keeps availability out of config
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
    if _is_away():
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
