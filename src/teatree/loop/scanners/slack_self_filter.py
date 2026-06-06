"""Self-message filter for Slack DM scanners (#1346).

The lowest common helper that BOTH downstream consumers of
:class:`PendingChatInjection` inherit:

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
hook injects them as user replies.

This module owns the filter at write-time — applied inside
:class:`SlackDmInboundScanner.scan` so rows that fail it never reach the
DB and both downstream consumers benefit for free.

**Fail-closed.** When the bot's own identity cannot be resolved
(``auth.test`` returned ``ok:false``, no bot token configured, transport
error), :func:`resolve_own_identity` returns ``None`` and
:func:`filter_self_messages` returns ``None`` to signal "identity
unknown — caller must NOT proceed". The scanner refuses to enqueue any
row that turn — better silent for one tick than spam-spawning
``t3:answerer`` sub-agents against the bot's own traffic.
"""

import logging
from dataclasses import dataclass

from teatree.core.backend_protocols import MessagingBackend
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)


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


def is_self_authored(message: RawAPIDict, identity: OwnSlackIdentity) -> bool:
    """True iff *message* was authored by the bot itself.

    Matches either ``message['user'] == identity.user_id`` (the bot's
    posted-as user id) or ``message['bot_id'] == identity.bot_id``
    (the bot id Slack stamps on bot-authored events). A match on either
    field is sufficient — bot-style messages don't always carry both.
    """
    user = message.get("user")
    if identity.user_id and isinstance(user, str) and user == identity.user_id:
        return True
    bot_id = message.get("bot_id")
    return bool(identity.bot_id and isinstance(bot_id, str) and bot_id == identity.bot_id)


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


__all__ = [
    "OwnSlackIdentity",
    "filter_self_messages",
    "is_self_authored",
    "resolve_own_identity",
]
