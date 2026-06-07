"""``SlackBotBackend`` — Slack messaging via the Web API.

Implements :class:`teatree.core.backend_protocols.MessagingBackend`. Outbound posts,
reactions, and user-id resolution use the Web API directly via httpx; inbound
``fetch_mentions`` / ``fetch_dms`` stream live events through Socket Mode and
are wired up by the ``t3 setup slack-bot`` walkthrough (see BLUEPRINT § 3.6).

The Phase 3 surface is intentionally minimal: enough for outbound routing
from the loop's scanners, with the Socket Mode receiver delivering inbound
events into the same backend through a queue managed by Phase 3.6.

Slack-Connect externally-shared channels reject the bot token with
``mcp_externally_shared_channel_restricted`` — both for reactions and
for ``chat.postMessage``.  When the human user's OAuth token (``xoxp-…``)
is configured via ``user_token_ref`` in ``~/.teatree.toml``, a single
deterministic policy (:meth:`SlackBotBackend._channel_token`) routes
*every* outbound surface — ``post_message``, ``post_reply``, ``react``,
``get_reactions`` — through the user token when, and only when, the
target is an externally-shared channel.  Connect membership is resolved
deterministically via ``conversations.info`` (``is_ext_shared`` /
``is_shared``), cached per channel id — never a try-bot-then-fallback
error probe.  DMs and ordinary internal channels keep the bot token
(those are scoped to the bot's own IM channels and cache; routing them
through ``xoxp`` would impersonate the user against their own history).
``conversations.open`` and ``users.lookupByEmail`` are always bot-token.

Reads-fail-safe-to-bot, writes-fail-toward-user (#1110). When a
``conversations.info`` probe cannot confirm membership (``ok:false`` —
bad token, missing scope, not-found, rate-limit), :meth:`_is_ext_shared`
returns ``None`` (unknown), not a silent ``False``. The operation then
decides: a *read* (history / metadata) falls safe to the bot token —
reads fail safe to the bot, since a bot-token read of an unreachable
Connect channel is empty at worst (the #1084 dedup tolerates that); a
*write* (``post_message`` / ``post_reply`` / ``react``, plus
``get_reactions`` and the #1084 dedup guard's read-as-the-post) fails
*toward* the user ``xoxp`` token — writes / reactions in a shared or
ambiguous context fail toward the user xoxp, never silently to the bot
(which a Connect channel rejects, dropping the partner write). Only
confirmed membership (``True`` / ``False``) is cached; an unknown is
re-probed on the next call so a transient failure that recovers
resolves correctly.
"""

import threading
from pathlib import Path
from typing import cast

from teatree.backends.slack.bot_errors import GLOBAL_TOKEN_FAILURES as _GLOBAL_TOKEN_FAILURES
from teatree.backends.slack.dm_history import read_single_message, read_thread_replies, read_user_dms
from teatree.backends.slack.http import SlackHttpClient
from teatree.backends.slack.react_errors import SingleEmojiBodyRefusedError, is_single_emoji_body
from teatree.backends.slack.scopes import OAUTH_SCOPES_HEADER, attach_granted_scopes
from teatree.backends.slack.self_identity import OwnSlackIdentity, resolve_own_identity, strip_self_audio_attachments
from teatree.backends.slack.token_policy import SlackOp, channel_token
from teatree.backends.slack.token_validation import (
    TokenSlotMismatchError,
    assert_app_token,
    assert_bot_token,
    assert_user_token,
    resolve_user_token_or_degrade,
)
from teatree.backends.slack.upload_response import shared_message_ts
from teatree.backends.slack.voice_classifier import ClassifierMode as VoiceClassifierMode
from teatree.backends.slack.voice_classifier import SlackVoiceMismatchError, VoiceTokenGate
from teatree.types import RawAPIDict, ScannerError

__all__ = [
    "SingleEmojiBodyRefusedError",
    "SlackBotBackend",
    "SlackOp",
    "SlackVoiceMismatchError",
    "TokenSlotMismatchError",
    "VoiceClassifierMode",
]


type SlackPayload = dict[str, object]


class _TickFanoutQueue:
    """Thread-safe inbound event buffer read non-destructively within a tick.

    The Socket Mode receiver calls :meth:`enqueue`; every scanner that
    shares one backend calls :meth:`snapshot` in the same tick. A
    destructive drain would let whichever scanner runs first consume the
    batch and leave the others with nothing — for DMs and reactions that
    means the RED CARD scanner falls back to degraded polling and can miss
    a real signal (#1655). :meth:`snapshot` instead returns a copy, so each
    of the concurrently-scheduled scanners observes the same events.

    The defined clear point is the first :meth:`enqueue` after any
    :meth:`snapshot`: a fresh event begins a new tick's batch and drops the
    already-served one, bounding the buffer at one tick's worth of events.
    Re-serving the same batch across consecutive no-arrival ticks is
    idempotent — the consuming scanners dedup on Slack ``ts`` / ``event_ts``
    in their persistence layer.
    """

    def __init__(self) -> None:
        self._events: list[RawAPIDict] = []
        self._served = False
        self._lock = threading.Lock()

    def enqueue(self, event: RawAPIDict) -> None:
        with self._lock:
            if self._served:
                self._events = []
                self._served = False
            self._events.append(event)

    def snapshot(self) -> list[RawAPIDict]:
        with self._lock:
            self._served = True
            return list(self._events)


class _SlackInbound:
    """Socket Mode inbound ingestion for one backend.

    The Phase 3.6 Socket Mode receiver pushes ``app_mention`` /
    ``message.im`` / ``reaction_added`` events through :meth:`enqueue_mention`
    / :meth:`enqueue_dm` / :meth:`enqueue_reaction`; the loop scanners read
    each per-tick batch through :meth:`snapshot_mentions` / :meth:`snapshot_dms`
    / :meth:`snapshot_reactions`. Reads are non-destructive within a tick so
    the scanners that share one backend each observe the same batch (#1655).
    Bundling the three queues and their ingestion behind one collaborator
    keeps the inbound concern out of the outbound messaging surface.
    """

    def __init__(self) -> None:
        self._mentions = _TickFanoutQueue()
        self._dms = _TickFanoutQueue()
        self._reactions = _TickFanoutQueue()

    def enqueue_mention(self, event: RawAPIDict) -> None:
        self._mentions.enqueue(event)

    def enqueue_dm(self, event: RawAPIDict) -> None:
        self._dms.enqueue(event)

    def enqueue_reaction(self, event: RawAPIDict) -> None:
        self._reactions.enqueue(event)

    def snapshot_mentions(self) -> list[RawAPIDict]:
        return self._mentions.snapshot()

    def snapshot_dms(self) -> list[RawAPIDict]:
        return self._dms.snapshot()

    def snapshot_reactions(self) -> list[RawAPIDict]:
        return self._reactions.snapshot()


class SlackBotBackend:  # noqa: PLR0904 — method count reflects the MessagingBackend Protocol surface, not poor encapsulation.
    """MessagingBackend backed by a Slack bot token, optionally with a user token.

    ``bot_token`` (``xoxb-…``) authorises Web API calls for DMs, posts, and
    bot-scoped lookups.
    ``app_token`` (``xapp-…``) authorises Socket Mode and is consumed by the
    Phase 3.6 walkthrough — kept on the instance so the bot starter can pick
    it up without a second config read.
    ``user_token`` (``xoxp-…``) authorises every outbound call (posts and
    reactions) in Slack-Connect externally-shared channels where the bot
    token is rejected by the workspace restriction policy.  When unset,
    every call falls back to the bot token.
    ``user_id`` is the Slack user id of the human the bot speaks for; scanners
    use it to filter @mentions targeted at that user.

    ``degrade_bad_user_token`` makes a prefix-mismatched ``user_token``
    degrade to bot-only (treated as absent, with a one-time WARNING)
    instead of raising. The loop construction paths set it so a Slack-only
    credential typo never wedges merges, CI, or PR sweeps; the explicit
    setup/provision path leaves it ``False`` so a wrong paste errors loudly.
    """

    def __init__(  # noqa: PLR0913 — Slack credential facade; each kwarg is a distinct documented token/identity/config slot, not an internal design smell.
        self,
        *,
        bot_token: str = "",
        app_token: str = "",
        user_token: str = "",
        user_id: str = "",
        dm_channel_id: str = "",
        degrade_bad_user_token: bool = False,
    ) -> None:
        # Construction-chokepoint prefix validation (codex #1282 item 5):
        # bot/app strict; user token degrades per ``degrade_bad_user_token``.
        assert_bot_token(bot_token)
        if degrade_bad_user_token:
            user_token = resolve_user_token_or_degrade(user_token)
        else:
            assert_user_token(user_token)
        assert_app_token(app_token)
        self._bot_token = bot_token
        self._app_token = app_token
        self._user_token = user_token
        self._user_id = user_id
        # Pre-provisioned IM channel id (#1342). When a per-overlay bot is
        # registered through ``t3 setup``, the setup-time provisioner calls
        # ``conversations.open`` once and persists the resulting channel id
        # in ``~/.teatree.toml`` under ``[overlays.<name>] slack_dm_channel_id``.
        # Threading it here short-circuits every subsequent ``open_dm(user_id)``
        # for the configured user so DMs route through this bot's IM rather
        # than failing ``channel_not_found`` (which previously caused silent
        # fallback through whichever bot already had an IM with the user —
        # the per-overlay attribution leak the issue reports).
        self._dm_channel_id = dm_channel_id
        self._http = SlackHttpClient()
        # #1395 voice/token gate; factory overrides via set_voice_classifier_mode.
        self._voice_gate = VoiceTokenGate(mode=VoiceClassifierMode.WARN, dm_channel_id=dm_channel_id)
        # The bot's own (user_id, bot_id) identity, resolved once via
        # ``auth.test``. The single bot-identity cache the backend reads for
        # the #1346 DM self-drop and the #2089 own-TTS-audio strip. ``False``
        # is the resolved-to-unknown sentinel so a failed probe is not re-run
        # every read; an unresolved identity fails open (no strip).
        self._cached_own_identity: OwnSlackIdentity | bool | None = None
        # Per-channel Slack-Connect membership, resolved once via
        # ``conversations.info`` then reused by the token-selection policy.
        self._ext_shared_cache: dict[str, bool] = {}
        # Inbound queues populated by the Phase 3.6 Socket Mode receiver. Each
        # tick the loop scanners read them via ``fetch_mentions`` /
        # ``fetch_dms`` / ``fetch_reactions``; the receiver calls
        # ``inbound.enqueue_mention`` / ``inbound.enqueue_dm`` /
        # ``inbound.enqueue_reaction``. Reads are non-destructive within a
        # tick so the three scanners that share one backend each see the same
        # batch (#1655).
        self._inbound = _SlackInbound()

    @property
    def app_token(self) -> str:
        return self._app_token

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    def user_token(self) -> str:
        return self._user_token

    def set_voice_classifier_mode(self, mode: VoiceClassifierMode) -> None:
        """Override the voice/token classifier mode (#1395)."""
        self._voice_gate.mode = mode

    def resolve_channel_token(self, channel: str) -> str:
        """The token an outbound post to *channel* would use (#1084).

        Public accessor over the single token-selection policy so the
        review-request dedup guard reads channel history with the exact
        token the post will use — read-token == post-token. The guard's
        live read is *taken as the post*, so it routes under
        ``SlackOp.WRITE``: a Connect channel rejects the bot token for
        both posting and reading, so the dedup read must use whatever the
        post will (the user ``xoxp`` on a Connect / ambiguous channel),
        not fail safe to a bot token the channel rejects (#1110). Connect
        membership is probed once (cached) only when both credentials
        exist, identical to ``post_message``'s routing.
        """
        return self._channel_token(channel, op=SlackOp.WRITE)

    @property
    def inbound(self) -> _SlackInbound:
        """Socket Mode ingestion surface (#1655).

        The receiver pushes events via ``inbound.enqueue_mention`` /
        ``enqueue_dm`` / ``enqueue_reaction``; ``fetch_mentions`` /
        ``fetch_dms`` / ``fetch_reactions`` read the per-tick batches back.
        """
        return self._inbound

    def _post(self, method: str, payload: SlackPayload, *, token: str = "", idempotent: bool = True) -> RawAPIDict:
        """POST *method* through the bounded-retry transport.

        ``idempotent`` gates the response-phase retry. A non-idempotent
        ``chat.postMessage`` (``idempotent=False``) is never replayed on a
        response-phase failure — a ``ReadTimeout`` after the request reached
        Slack may mean it already posted. Replayable calls (``reactions.add``'s
        ``already_reacted`` no-op, ``auth.test``, ``conversations.*`` lookups)
        keep the ``True`` default.
        """
        auth = token or self._bot_token
        if not auth:
            return {}
        return self._http.post(method, token=auth, json=payload, idempotent=idempotent)

    def _get(self, method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
        auth = token or self._bot_token
        if not auth:
            return {}
        return self._http.get(method, token=auth, params=params)

    def _is_ext_shared(self, channel: str) -> bool | None:
        """Whether *channel* is a Slack-Connect externally-shared channel.

        Resolved deterministically from ``conversations.info``
        (``is_ext_shared`` / ``is_shared``) on the bot token — the bot
        can always *read* channel metadata even on channels it cannot
        *post* to.  Returns ``True`` (confirmed externally-shared) or
        ``False`` (confirmed internal) only when the API call succeeds
        (``ok:true``).  On any API-level lookup failure (``ok:false`` —
        bad token, missing scope, channel not found, rate-limit) it
        returns ``None`` (membership *unknown*) so the policy decides by
        operation class — reads fail safe to the bot, writes/reactions
        fail toward the user ``xoxp`` (#1110).  Only the confirmed
        ``True`` / ``False`` answer is cached per channel id; an unknown
        is **not** cached, so a transient failure that later recovers is
        re-probed and resolves correctly.  A transport-level failure
        (5xx / connection error) instead propagates out of ``_get``'s
        ``raise_for_status()`` through ``_channel_token`` and aborts the
        call — still conservative (no wrong-token send), but the call
        does not complete.
        """
        cached = self._ext_shared_cache.get(channel)
        if cached is not None:
            return cached
        data = self._get("conversations.info", {"channel": channel})
        if not data.get("ok"):
            return None
        info = cast("RawAPIDict", data.get("channel") or {})
        is_ext = bool(info.get("is_ext_shared")) or bool(info.get("is_shared"))
        self._ext_shared_cache[channel] = is_ext
        return is_ext

    def _channel_token(self, channel: str, *, op: SlackOp) -> str:
        """The token authorising an outbound *op* against *channel*.

        The single, deterministic token-selection policy consulted by
        every outbound surface — ``post_message`` / ``post_reply`` /
        ``react`` / ``get_reactions`` (all ``SlackOp.WRITE``) — and, via
        the shared
        :func:`teatree.backends.slack.token_policy.channel_token` helper,
        by the review-request dedup guard's read-as-the-post
        (``resolve_channel_token`` → ``SlackOp.WRITE``, so read-token ==
        post-token #1084). Connect membership is only probed when both
        credentials exist (the helper short-circuits the single-token /
        DM cases first), preserving the legacy no-probe behaviour. When
        the probe cannot confirm membership the policy falls back by
        ``op``: a ``READ`` fails safe to the bot token (a bot-token read
        of an unreachable Connect channel is empty at worst), a ``WRITE``
        fails toward the user ``xoxp`` token (the bot token is rejected
        on a Connect channel and the partner write is silently dropped).
        """
        if not self._user_token or not self._bot_token or channel.startswith("D"):
            return channel_token(
                channel,
                bot_token=self._bot_token,
                user_token=self._user_token,
                is_ext_shared=False,
                op=op,
            )
        return channel_token(
            channel,
            bot_token=self._bot_token,
            user_token=self._user_token,
            is_ext_shared=self._is_ext_shared(channel),
            op=op,
        )

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        """Return queued Socket Mode mentions in order, non-destructively.

        ``since`` is accepted for protocol compatibility but ignored — the
        Socket Mode receiver delivers events in real time, so the queue
        only ever holds events that arrived after the previous tick. The
        read is a snapshot so every scanner sharing this backend sees the
        same batch within a tick; the buffer rolls on the next enqueue
        (#1655).
        """
        _ = since
        return self._inbound.snapshot_mentions()

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        """Return new DMs from the user, including thread replies.

        Reads the Socket Mode queue first (populated by a running
        receiver). The read is non-destructive within a tick so the three
        scanners that share one backend — ``SlackDmInboundScanner``,
        ``SlackMentionsScanner``, ``RedCardScanner`` — each see the same
        batch instead of racing a destructive drain that left the losers on
        degraded polling and could miss a RED CARD (#1655).

        When the queue is empty, falls back to polling
        ``conversations.history`` on the bot's DM channel with the
        configured user, then for every top-level bot message also polls
        ``conversations.replies`` so thread replies are picked up (#1044).

        Slack's ``conversations.history`` and ``conversations.replies``
        do not stamp the ``channel`` field on each message — it is the
        request parameter, not part of the response. We stamp it here so
        downstream consumers (``SlackDmInboundScanner`` →
        ``PendingChatInjection.record``) have it (#1043).

        Only messages FROM the user are returned (bot's own messages are
        filtered out).
        """
        queued = self._inbound.snapshot_dms()
        if queued:
            # Real-time Socket-Mode events: the bot's own posts are already
            # dropped downstream (receiver drops bot_message; the inbound
            # scanner applies filter_self_messages). The queue short-circuit
            # must not call Slack (#1655), so no auth.test probe here.
            return queued
        if not self._user_id or not self._bot_token:
            return []
        channel = self.open_dm(self._user_id)
        if not channel:
            return []
        collected = read_user_dms(get=self._get, channel=channel, since=since, identity=self._own_identity())
        return self._strip_own_tts_audio(collected)

    def fetch_reactions(self, *, since: str = "") -> list[RawAPIDict]:
        """Return queued Socket Mode ``reaction_added`` events, non-destructively.

        ``since`` is accepted for protocol compatibility but ignored — the
        Socket Mode receiver delivers events in real time so the queue
        only holds events that arrived after the previous tick. The read is
        a snapshot so ``SlackReviewIntentScanner`` and ``RedCardScanner``
        each see the same batch within a tick rather than the first one
        draining it (#1655); the buffer rolls on the next enqueue.
        """
        _ = since
        return self._inbound.snapshot_reactions()

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        """Fetch a single message by ``(channel, ts)``.

        ``{}`` on any failure or no match. Read body in
        :mod:`~teatree.backends.slack.dm_history`; own TTS audio stripped
        at the backend read chokepoint.
        """
        message = read_single_message(get=self._get, channel=channel, ts=ts)
        if not message:
            return {}
        [stripped] = self._strip_own_tts_audio([message])
        return stripped

    def fetch_thread_replies(self, *, channel: str, thread_ts: str) -> list[RawAPIDict]:
        """Return every message in the thread rooted at ``thread_ts`` (#2061).

        Keyed on the thread ROOT — a reply re-parents to the root, so the
        answer pipeline's dedup/verification read-back must query the root,
        not a non-root user-message ts. Read body in
        :mod:`~teatree.backends.slack.dm_history`; own TTS audio stripped here.
        """
        return self._strip_own_tts_audio(read_thread_replies(get=self._get, channel=channel, thread_ts=thread_ts))

    def fetch_channel_history(self, *, channel: str, limit: int = 50) -> list[RawAPIDict]:
        """Return the most recent *limit* messages in *channel* (#1255).

        Used by :class:`SlackBroadcastsScanner` to poll review-broadcast
        channels for MR URLs. This is a "read taken as the post" — the
        scanner will later react on these messages — so it routes
        through ``_channel_token`` with :attr:`SlackOp.WRITE`. On a
        Slack-Connect channel the bot token is rejected for *both*
        history reads and reactions with
        ``mcp_externally_shared_channel_restricted``; the WRITE op
        falls toward the user ``xoxp`` token in the ambiguous case
        (``conversations.info`` itself fails because the bot has no
        access to the Connect channel) and uses ``xoxp`` for confirmed
        ext-shared channels, matching the token ``post_message`` /
        ``react`` will go out under. A bot-token history read on a
        Connect channel returns empty, which would silently drop every
        broadcast — using the WRITE op keeps read-token == post-token,
        the load-bearing invariant from #1084. Falls back to ``[]`` on
        any non-ok response so one slow channel never breaks the scan
        loop. ``channel`` is stamped on each message so downstream
        consumers don't have to thread it back in.
        """
        if not channel:
            return []
        token = self._channel_token(channel, op=SlackOp.WRITE)
        data = self._get(
            "conversations.history",
            {"channel": channel, "limit": max(1, min(limit, 200))},
            token=token,
        )
        if not data.get("ok"):
            error_code = str(data.get("error", ""))
            # Global token failures (auth / missing scope / rate limit /
            # deactivated) suppress every Slack scan — raise so the
            # dispatcher records the error and DMs the user (#1287).
            # Channel-scoped failures (``channel_not_found``,
            # ``not_in_channel``, ``is_archived``) stay quiet per the
            # #1255 "one slow channel never breaks the scan loop" design.
            if error_code in _GLOBAL_TOKEN_FAILURES:
                raise ScannerError(
                    scanner="slack_broadcasts",
                    error_class=_GLOBAL_TOKEN_FAILURES[error_code],
                    detail=f"conversations.history on {channel}: {error_code}",
                )
            return []
        messages = data.get("messages")
        if not isinstance(messages, list):
            return []
        out: list[RawAPIDict] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            entry = cast("RawAPIDict", msg)
            entry.setdefault("channel", channel)
            out.append(entry)
        return self._strip_own_tts_audio(out)

    def _own_identity(self) -> OwnSlackIdentity | None:
        """The bot's own ``(user_id, bot_id)``, resolved once via ``auth.test``.

        The single bot-identity resolver for the backend: the #1346 DM
        self-drop (``_collect_user_dms``) and the #2089 own-TTS-audio strip
        (``_strip_own_tts_audio``) both read it, so the bot's identity costs at
        most one ``auth.test`` call per backend. ``False`` is the
        resolved-to-unknown sentinel so a failed probe is not re-run every
        read; an unresolved identity fails open.
        """
        if self._cached_own_identity is None:
            self._cached_own_identity = resolve_own_identity(self) or False
        resolved = self._cached_own_identity
        return resolved if isinstance(resolved, OwnSlackIdentity) else None

    def _strip_own_tts_audio(self, messages: list[RawAPIDict]) -> list[RawAPIDict]:
        """Drop the bot's OWN TTS audio attachment from every read message (#2089).

        The single Slack-read chokepoint: every ``conversations.*`` read the
        backend surfaces passes through here so the loop never re-ingests a
        spoken copy of text the bot already wrote. User-authored audio (voice
        notes) is preserved — :func:`strip_self_audio_attachments` only strips
        attachments on the bot's own messages.
        """
        return strip_self_audio_attachments(messages, self._own_identity())

    def auth_test(self) -> RawAPIDict:
        """Return the ``auth.test`` body with granted scopes from the ``X-OAuth-Scopes`` header.

        Slack reports the token's scopes in the response header, not the JSON
        body; they are attached under :data:`GRANTED_SCOPES_KEY` (native keys
        untouched) so a connector-preflight scope guard can read them. ``{}``
        when no bot token is configured.
        """
        if not self._bot_token:
            return {}
        body, scopes_header = self._http.post_with_header(
            "auth.test",
            token=self._bot_token,
            json={},
            header=OAUTH_SCOPES_HEADER,
        )
        return attach_granted_scopes(body, scopes_header)

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        if is_single_emoji_body(text):
            raise SingleEmojiBodyRefusedError(text)
        token = self._channel_token(channel, op=SlackOp.WRITE)
        self._voice_gate.check(text=text, channel=channel, token=token)
        payload: SlackPayload = {"channel": channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return self._post("chat.postMessage", payload, token=token, idempotent=False)

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        if is_single_emoji_body(text):
            raise SingleEmojiBodyRefusedError(text)
        token = self._channel_token(channel, op=SlackOp.WRITE)
        self._voice_gate.check(text=text, channel=channel, token=token)
        return self._post(
            "chat.postMessage",
            {"channel": channel, "thread_ts": ts, "text": text},
            token=token,
            idempotent=False,
        )

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        return self._post(
            "reactions.add",
            {"channel": channel, "timestamp": ts, "name": emoji},
            token=self._channel_token(channel, op=SlackOp.WRITE),
        )

    def _is_self_dm(self, channel: str) -> bool:
        """True when *channel* is the configured user's own DM (#1750).

        The single deterministic destination test for the #1750 routing
        rule. The user's own IM is the channel id provisioned at
        ``t3 setup`` time (:attr:`_dm_channel_id`), or — when an ``open_dm``
        has not yet been resolved — the user's own ``U…`` id, which Slack
        accepts as a ``chat.postMessage`` target that opens/uses the
        self-IM. A *colleague's* DM is a different ``D…`` id and is
        therefore NOT a self-DM, so it routes to ``xoxp`` like any other
        non-self surface.
        """
        if not channel:
            return False
        if self._dm_channel_id and channel == self._dm_channel_id:
            return True
        return bool(self._user_id) and channel == self._user_id

    def _route_token(self, channel: str) -> str:
        """The token a #1750-routed post/react to *channel* goes out under.

        The single, deterministic destination router shared by
        :meth:`post_routed` and :meth:`react_routed` (reacting follows the
        same rule as posting). A private message *to the user* — the user's
        own DM — goes through the per-overlay **bot** (``xoxb``); a message
        or reaction to a *colleague* or a *channel* goes out under the
        user's personal **OAuth** (``xoxp``) token. Distinct from
        :meth:`_channel_token`, which is the Connect-membership policy that
        keeps confirmed-internal channels (and *all* ``D…`` DMs) on the
        bot — that policy cannot tell a colleague DM from the self DM,
        which is exactly the distinction #1750 turns on.

        Falls back to whichever single credential is configured when the
        other is absent, so a bot-only or user-only deployment still has a
        usable token.
        """
        if self._is_self_dm(channel):
            return self._bot_token or self._user_token
        return self._user_token or self._bot_token

    def route_token(self, channel: str) -> str:
        """Public accessor over the #1750 destination router (self-DM→bot, else→xoxp).

        The deterministic classifier ``post_routed`` / ``react_routed`` and
        :class:`~teatree.core.on_behalf_egress.OnBehalfSlackEgress` consult to
        choose the outbound token by destination (and as the self-DM carve-out
        presence probe). Distinct from :meth:`resolve_channel_token`, the
        Connect-membership policy that cannot tell the user's own DM from a
        colleague's.
        """
        return self._route_token(channel)

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        """Post to *channel*, token chosen by destination (#1750).

        The deterministic edge for ``t3 <overlay> notify post``: routes
        through :meth:`_route_token` (self-DM → bot, colleague/channel →
        ``xoxp``). Returns the raw Slack body so the CLI can inspect
        ``ok`` / ``error``; ``{}`` when no token at all is configured.
        """
        token = self._route_token(channel)
        if not token:
            return {}
        payload: SlackPayload = {"channel": channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return self._post("chat.postMessage", payload, token=token, idempotent=False)

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        """Add a reaction to *channel*'s message, token chosen by destination (#1750).

        Reacting follows the *same* :meth:`_route_token` rule as
        :meth:`post_routed` — self-DM → bot, colleague/channel → ``xoxp``.
        Returns the raw Slack body; ``{}`` when no token is configured.
        """
        token = self._route_token(channel)
        if not token:
            return {}
        return self._post(
            "reactions.add",
            {"channel": channel, "timestamp": ts, "name": emoji},
            token=token,
        )

    def open_dm(self, user_id: str) -> str:
        """Return the IM channel id for *user_id*; short-circuit to the cached id when set (#1342)."""
        if user_id and user_id == self._user_id and self._dm_channel_id:
            return self._dm_channel_id
        data = self._post("conversations.open", {"users": user_id})
        if not data.get("ok"):
            return ""
        channel = cast("RawAPIDict", data.get("channel") or {})
        channel_id = channel.get("id")
        return channel_id if isinstance(channel_id, str) else ""

    def join_conversation(self, channel: str) -> RawAPIDict:
        """Join the bot to a public channel via ``conversations.join`` (bot token).

        Returns the raw Slack body. ``ok:true`` is returned both on a fresh
        join and when the bot is already a member (Slack sets
        ``already_in_channel``), so callers treat the call as idempotent. A
        private or Slack-Connect channel rejects a self-join with an error in
        the body; the setup-time channel provisioner maps that to a clean
        "invite the bot manually" instruction rather than failing.
        """
        if not channel:
            return {}
        return self._post("conversations.join", {"channel": channel})

    def get_permalink(self, *, channel: str, ts: str) -> str:
        """Return the archive permalink for ``(channel, ts)`` or ``""``."""
        if not channel or not ts:
            return ""
        data = self._get("chat.getPermalink", {"channel": channel, "message_ts": ts})
        if not data.get("ok"):
            return ""
        permalink = data.get("permalink", "")
        return permalink if isinstance(permalink, str) else ""

    def post_audio_dm(
        self,
        *,
        channel: str,
        filepath: str,
        text: str,
        thread_ts: str = "",
        title: str = "",
    ) -> RawAPIDict:
        """Post ONE DM to ``channel`` carrying ``text`` + an inline audio attachment (#2050).

        The modern three-step upload (``files.upload`` is deprecated):
        ``getUploadURLExternal`` reserves an off-Slack ``upload_url`` + file
        ``id``; the bytes are POSTed there; ``completeUploadExternal`` shares
        the file into ``channel_id`` with ``text`` as the ``initial_comment``
        and, when set, ``thread_ts`` — a SINGLE DM (text + inline player).

        Finalising requires the token's ``files:write`` scope; without it the
        reserve step returns ``ok:false`` / ``missing_scope`` (surfaced
        verbatim so the caller degrades to a text-only post). Routes under
        :meth:`_route_token`. Returns the raw ``completeUploadExternal`` body
        (``{}`` when no token is configured or the file is unreadable).
        """
        token = self._route_token(channel)
        if not token or not channel:
            return {}
        path = Path(filepath)
        try:
            content = path.read_bytes()
        except OSError:
            return {}
        reserve = self._get(
            "files.getUploadURLExternal",
            {"filename": path.name, "length": len(content)},
            token=token,
        )
        if not reserve.get("ok"):
            return reserve
        upload_url = reserve.get("upload_url")
        file_id = reserve.get("file_id")
        if not isinstance(upload_url, str) or not isinstance(file_id, str):
            return reserve
        self._http.post_external(upload_url, content=content)
        file_entry: RawAPIDict = {"id": file_id}
        if title:
            file_entry["title"] = title
        payload: RawAPIDict = {"files": [file_entry], "channel_id": channel, "initial_comment": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        body = self._post("files.completeUploadExternal", payload, token=token, idempotent=False)
        if shared_ts := shared_message_ts(body, channel=channel):
            body["ts"] = shared_ts
        return body

    def get_reactions(self, *, channel: str, ts: str) -> list[str]:
        """Return the emoji names currently set on a message."""
        data = self._get(
            "reactions.get",
            {"channel": channel, "timestamp": ts},
            token=self._channel_token(channel, op=SlackOp.WRITE),
        )
        if not data.get("ok"):
            return []
        message = cast("RawAPIDict", data.get("message") or {})
        reactions = message.get("reactions")
        if not isinstance(reactions, list):
            return []
        names: list[str] = []
        for raw_reaction in reactions:
            if not isinstance(raw_reaction, dict):
                continue
            reaction = cast("RawAPIDict", raw_reaction)
            name = reaction.get("name")
            if isinstance(name, str):
                names.append(name)
        return names

    def resolve_user_id(self, handle: str) -> str:
        """Look up a Slack user id from a handle (``@alice`` or ``alice``)."""
        clean = handle.lstrip("@")
        if not clean:
            return ""
        data = self._get("users.lookupByEmail", {"email": clean}) if "@" in clean else {}
        if data.get("ok"):
            user = cast("RawAPIDict", data.get("user") or {})
            user_id = user.get("id")
            if isinstance(user_id, str):
                return user_id
        # Fallback: list users and match by name. Cheap for personal workspaces;
        # the loop scanners cache the result via ``functools.lru_cache`` upstream.
        listing = self._get("users.list", {"limit": 200})
        members = listing.get("members")
        if not isinstance(members, list):
            return ""
        for raw_member in members:
            if not isinstance(raw_member, dict):
                continue
            member = cast("RawAPIDict", raw_member)
            if member.get("name") == clean or member.get("real_name") == clean:
                user_id = member.get("id")
                if isinstance(user_id, str):
                    return user_id
        return ""
