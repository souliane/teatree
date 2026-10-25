"""Self-message primitives for Slack DM scanners (#1346 / #2089 / #1941).

The scanner-facing import surface. The bot-identity logic and the
self-message transforms live in the backend layer
(:mod:`teatree.backends.slack.self_identity`) so
:class:`~teatree.backends.slack.bot.SlackBotBackend` can apply them at its
read chokepoint without a backwards import to this orchestration layer. This
module re-exports them so the loop scanners keep one stable import path.

:func:`filter_self_messages` is the shared recorder guard. The recorder admits
an app-posted owner reply only when it belongs to a recorded card thread;
other app posts still fail :func:`is_on_behalf_posted`'s check.
The reactive Slack-answer cycle consumes only the rows the recorder admits.

The Slack Socket Mode receiver only drops ``subtype=bot_message`` events;
the bot's own outbound posts from ``chat.postMessage`` arrive as plain
``message`` events whose ``user`` matches the bot's posted-as user id and
whose ``bot_id`` matches the bot's bot id. Without a self-filter the bot
ends up "answering" its own outbound DMs (#1346). The recorder applies this
identity filter and its scoped app-post check before a row reaches the DB.

**Fail-closed (identity filter only).** When the bot's own identity cannot
be resolved (``auth.test`` returned ``ok:false``, no bot token configured,
transport error), :func:`resolve_own_identity` returns ``None`` and
:func:`filter_self_messages` returns ``None`` to signal "identity
unknown — caller must NOT proceed". The recorder refuses to enqueue any
row that turn — better silent for one tick than spam-spawning
``t3:answerer`` sub-agents against the bot's own traffic.

**On-behalf filter (#1941, structural).** An automated on-behalf post
sent with the human's OWN Slack token carries the human's own ``user``
id — not the bot's — so :func:`filter_self_messages` never catches it.
The recorder checks ``api_app_id`` and admits only owner-authored replies
in recorded owner-card threads; other app posts remain filtered.
"""

from teatree.backends.slack.self_identity import (
    OwnSlackIdentity,
    drop_on_behalf_messages,
    filter_self_messages,
    is_on_behalf_posted,
    is_self_authored,
    is_tts_audio_file,
    resolve_own_identity,
    strip_self_audio_attachments,
)

__all__ = [
    "OwnSlackIdentity",
    "drop_on_behalf_messages",
    "filter_self_messages",
    "is_on_behalf_posted",
    "is_self_authored",
    "is_tts_audio_file",
    "resolve_own_identity",
    "strip_self_audio_attachments",
]
