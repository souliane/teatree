"""Self-message primitives for Slack DM scanners (#1346 / #2089).

The scanner-facing import surface. The bot-identity logic and the
self-message transforms live in the backend layer
(:mod:`teatree.backends.slack.self_identity`) so
:class:`~teatree.backends.slack.bot.SlackBotBackend` can apply them at its
read chokepoint without a backwards import to this orchestration layer. This
module re-exports them so the loop scanners keep one stable import path.

:func:`filter_self_messages` is the lowest common helper that BOTH downstream
consumers of :class:`PendingChatInjection` inherit:

* The reactive Slack-answer cycle (``run_slack_answer_cycle``) — which
    spawns ``t3:answerer`` sub-agents against unanswered rows.
* The ``UserPromptSubmit`` injection hook (``handle_inject_pending_chat``
    in ``hook_router.py``) — which surfaces unconsumed rows as
    ``additionalContext`` to the next interactive prompt.

The Slack Socket Mode receiver only drops ``subtype=bot_message`` events;
the bot's own outbound posts from ``chat.postMessage`` arrive as plain
``message`` events whose ``user`` matches the bot's posted-as user id and
whose ``bot_id`` matches the bot's bot id. Without a self-filter the bot
ends up "answering" its own outbound DMs (#1346) and the UserPromptSubmit
hook injects them as user replies. The filter is applied inside
:class:`SlackDmInboundScanner.scan` so rows that fail it never reach the DB
and both downstream consumers benefit for free.

**Fail-closed.** When the bot's own identity cannot be resolved
(``auth.test`` returned ``ok:false``, no bot token configured, transport
error), :func:`resolve_own_identity` returns ``None`` and
:func:`filter_self_messages` returns ``None`` to signal "identity
unknown — caller must NOT proceed". The scanner refuses to enqueue any
row that turn — better silent for one tick than spam-spawning
``t3:answerer`` sub-agents against the bot's own traffic.
"""

from teatree.backends.slack.self_identity import (
    OwnSlackIdentity,
    filter_self_messages,
    is_self_authored,
    is_tts_audio_file,
    resolve_own_identity,
    strip_self_audio_attachments,
)

__all__ = [
    "OwnSlackIdentity",
    "filter_self_messages",
    "is_self_authored",
    "is_tts_audio_file",
    "resolve_own_identity",
    "strip_self_audio_attachments",
]
