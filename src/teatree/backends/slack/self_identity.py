"""The bot's own Slack identity + self-message primitives (#1346 / #2089).

The single owner of "is this Slack message the bot's own?" logic and the
self-message transforms built on it. Lives in the backend layer (Slack
message-shape logic) so :class:`~teatree.backends.slack.bot.SlackBotBackend`
can apply them at its read chokepoint without a backwards import to the loop
layer; the loop scanner re-exports them via
:mod:`teatree.loop.scanners.slack_self_filter`.

Two transforms, two failure doctrines:

*   :func:`filter_self_messages` — DROP the bot's own DMs before they reach
    :class:`PendingChatInjection`, so the bot never "answers" its own outbound
    posts (#1346). **Fail-closed**: an unresolved identity returns ``None`` and
    the scanner refuses to enqueue any row that turn.
*   :func:`strip_self_audio_attachments` — STRIP the bot's own TTS audio
    attachment when reading Slack, so the loop never re-ingests a spoken copy
    of text it already wrote (#2089). **Fail-open**: an unresolved identity
    passes the batch through unchanged — the only cost is token waste, never a
    safety violation.
"""

import logging
from dataclasses import dataclass
from typing import cast

from teatree.core.backend_protocols import MessagingBackend
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

_AUDIO_FILETYPES: frozenset[str] = frozenset({"m4a", "mp4", "mp3", "ogg", "wav", "aac", "webm", "amr"})


@dataclass(frozen=True, slots=True)
class OwnSlackIdentity:
    """The bot's own Slack identity as seen on inbound message events.

    ``user_id`` is the ``U…`` value Slack uses for the bot's posted-as
    identity (matches ``message['user']`` on bot-authored events).
    ``bot_id`` is the ``B…`` value that Slack stamps on bot-authored
    events (matches ``message['bot_id']``). Either match is sufficient
    to classify a message as self-authored — a bot's outbound DM may
    carry only one of them depending on how it was posted.
    """

    user_id: str
    bot_id: str

    @property
    def is_resolvable(self) -> bool:
        """True iff at least one identifier is non-empty.

        An empty identity (both fields blank) cannot distinguish self
        from non-self — callers treat it the same as "could not
        resolve" and fail closed.
        """
        return bool(self.user_id or self.bot_id)


def resolve_own_identity(backend: MessagingBackend) -> OwnSlackIdentity | None:
    """Probe ``auth.test`` once and return the bot's own ids, or ``None``.

    ``None`` means "identity unknown" — the call returned ``ok:false``,
    the bot token is unconfigured (``auth_test`` returned ``{}``), or the
    transport raised. Callers (the scanner) treat this as a hard
    fail-closed signal and refuse to enqueue any row that turn.

    The Slack ``auth.test`` response shape:
    ``{"ok": true, "user_id": "U…", "bot_id": "B…", …}``. Either
    identifier in isolation is enough — bot-style messages don't always
    carry both — so the empty-string default for the missing field is
    intentional.
    """
    try:
        response = backend.auth_test()
    except Exception as exc:  # noqa: BLE001 — fail-closed on transport failure
        logger.warning("auth.test raised; cannot resolve own identity for self-filter: %s", exc)
        return None
    if not response or not response.get("ok"):
        return None
    user_id = response.get("user_id", "")
    bot_id = response.get("bot_id", "")
    if not isinstance(user_id, str):
        user_id = ""
    if not isinstance(bot_id, str):
        bot_id = ""
    identity = OwnSlackIdentity(user_id=user_id, bot_id=bot_id)
    if not identity.is_resolvable:
        return None
    return identity


def is_thread_root(message: RawAPIDict) -> bool:
    """True iff *message* is the root of its own thread.

    Slack marks a thread root with ``thread_ts == ts``. Used to decide
    whether to fan out to ``conversations.replies`` when reading DMs.
    """
    thread_ts = message.get("thread_ts")
    return isinstance(thread_ts, str) and bool(thread_ts) and thread_ts == message.get("ts")


def is_self_authored(message: RawAPIDict, identity: OwnSlackIdentity) -> bool:
    """True iff *message* was authored by the bot itself.

    A message is self-authored when its ``user`` or ``bot_id`` field equals
    any of the identity's known ids (``user_id`` and ``bot_id``). The union
    is intentional: ``auth.test`` may return only ``user_id`` (older/limited
    tokens), and Slack stamps that single identifier into a bot post's
    ``bot_id`` field — so matching either message field against either
    identity id catches every self-authored shape, where a strict
    field-to-field pairing would miss the limited-token case.
    """
    known_ids = {i for i in (identity.user_id, identity.bot_id) if i}
    if not known_ids:
        return False
    for field_value in (message.get("user"), message.get("bot_id")):
        if isinstance(field_value, str) and field_value in known_ids:
            return True
    return False


def filter_self_messages(
    messages: list[RawAPIDict],
    identity: OwnSlackIdentity | None,
) -> list[RawAPIDict] | None:
    """Drop self-authored messages from *messages*; ``None`` when fail-closed.

    Returns the filtered list when *identity* is resolved; returns
    ``None`` when *identity* is ``None`` so the caller can refuse to
    enqueue any row that turn (the fail-closed contract).
    """
    if identity is None:
        return None
    return [m for m in messages if not is_self_authored(m, identity)]


def is_tts_audio_file(file_entry: object) -> bool:
    """True iff a Slack ``files`` entry is an audio attachment (#2089).

    The Slack-TTS feature attaches a synthesised ``speech.m4a`` rendition
    of a message's own text. A file is audio when its ``mimetype`` starts
    ``audio/`` or its ``filetype`` is a known audio extension — both forms
    Slack stamps on uploaded audio. Accepts any ``files`` list element
    (Slack types them as ``object``) and returns ``False`` for non-dicts.
    """
    if not isinstance(file_entry, dict):
        return False
    entry = cast("RawAPIDict", file_entry)
    mimetype = entry.get("mimetype")
    if isinstance(mimetype, str) and mimetype.startswith("audio/"):
        return True
    filetype = entry.get("filetype")
    return isinstance(filetype, str) and filetype.lower() in _AUDIO_FILETYPES


def strip_self_audio_attachments(
    messages: list[RawAPIDict],
    identity: OwnSlackIdentity | None,
) -> list[RawAPIDict]:
    """Remove the bot's OWN audio attachments from each message (#2089).

    For a message authored by the bot itself (per :func:`is_self_authored`),
    drop every audio ``files`` entry — its content is a spoken copy of text
    already present in the same message, so re-ingesting it is token waste.
    Non-audio attachments and the message text are preserved; messages
    authored by OTHERS (the user's voice notes) are left untouched so the
    skip is not over-broad.

    Fail-open: when *identity* is ``None`` (bot identity unresolved) the
    batch passes through unchanged. The only cost of not stripping is token
    waste, never a safety violation, so fail-open is correct here (unlike
    :func:`filter_self_messages`, which fails closed to avoid answering the
    bot's own DMs).
    """
    if identity is None:
        return messages
    stripped: list[RawAPIDict] = []
    for message in messages:
        files = message.get("files")
        if not is_self_authored(message, identity) or not isinstance(files, list):
            stripped.append(message)
            continue
        kept = [f for f in files if not is_tts_audio_file(f)]
        if len(kept) == len(files):
            stripped.append(message)
            continue
        stripped.append({**message, "files": kept})
    return stripped


__all__ = [
    "OwnSlackIdentity",
    "filter_self_messages",
    "is_self_authored",
    "is_thread_root",
    "is_tts_audio_file",
    "resolve_own_identity",
    "strip_self_audio_attachments",
]
