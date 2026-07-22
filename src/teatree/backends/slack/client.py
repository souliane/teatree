from dataclasses import dataclass
from typing import cast

import httpx

from teatree.backends.slack.http import SlackHttpClient
from teatree.backends.slack.pagination import next_cursor
from teatree.identity import agent_signature_suffix
from teatree.types import RawAPIDict
from teatree.url_classify import find_pr_urls


def post_webhook_message(webhook_url: str, text: str, *, signature: str = "") -> RawAPIDict:
    """Post a Slack webhook message on the user's behalf.

    `signature` is appended only when the DB-home `agent_signature` setting is
    `true` (`t3 <overlay> config_setting set agent_signature true`); a
    `[teatree] agent_signature` TOML value is ignored on read. Default config
    keeps the message indistinguishable from one the user typed themselves —
    see `teatree.identity` and
    `skills/rules/SKILL.md` § "No AI Signature on Posts Made on the User's
    Behalf".
    """
    body = text + agent_signature_suffix(signature)
    response = httpx.post(webhook_url, json={"text": body}, timeout=10.0)
    response.raise_for_status()
    return cast("RawAPIDict", response.json())


@dataclass(frozen=True, slots=True)
class SlackReviewMatch:
    pr_url: str
    permalink: str
    channel: str
    ts: str = ""
    author: str = ""


def _resolve_workspace_domain(client: SlackHttpClient, token: str) -> str:
    """Resolve the Slack workspace domain via ``auth.test``, or ``""`` on failure.

    A failed ``auth.test`` (ratelimited, auth error, transport failure) must NOT
    fabricate ``app.slack.com`` — a wrong domain yields permalinks that point at
    the generic app host and never resolve to the real message. An empty domain
    is the honest signal that the permalink base is unknown.
    """
    data = client.get("auth.test", token=token, params={})
    if not data.get("ok"):
        return ""
    url = str(data.get("url", "")).rstrip("/")
    return url.removeprefix("https://") if url.startswith("https://") else url


@dataclass(frozen=True, slots=True)
class _ChannelContext:
    channel_id: str
    channel_name: str
    workspace_domain: str


def _iter_review_matches(
    msg: RawAPIDict,
    pr_url_set: set[str],
    seen: set[str],
    ctx: _ChannelContext,
) -> list[SlackReviewMatch]:
    """Extract SlackReviewMatch entries from a single Slack message."""
    text = str(msg.get("text", ""))
    ts = str(msg.get("ts", ""))
    if not ts:
        return []
    found_urls = find_pr_urls(text)
    if not found_urls:
        return []

    author = str(msg.get("user", "") or msg.get("bot_id", ""))
    permalink = f"https://{ctx.workspace_domain}/archives/{ctx.channel_id}/p{ts.replace('.', '')}"
    matches: list[SlackReviewMatch] = []
    for url in found_urls:
        clean_url = url.rstrip("/").split("#")[0]
        if clean_url in pr_url_set and clean_url not in seen:
            seen.add(clean_url)
            matches.append(
                SlackReviewMatch(
                    pr_url=clean_url,
                    permalink=permalink,
                    channel=ctx.channel_name,
                    ts=ts,
                    author=author,
                )
            )
    return matches


def _fetch_history_page(
    client: SlackHttpClient,
    token: str,
    channel_id: str,
    cursor: str | None,
    oldest_ts: str = "",
) -> RawAPIDict:
    """Fetch one page of ``conversations.history``. Returns {} on a non-ok body.

    Routes through :class:`SlackHttpClient` so a transient ``429`` / ``5xx`` is
    retried (honoring ``Retry-After``) instead of dropping the dedup read — a
    dropped read during a PR sweep would re-post a duplicate review request.
    ``oldest_ts`` bounds the read to messages at or after that Slack ``ts`` so a
    recency-windowed dedup never paginates the entire channel history.
    """
    params: dict[str, str | int] = {"channel": channel_id, "limit": 100}
    if oldest_ts:
        params["oldest"] = oldest_ts
    if cursor:
        params["cursor"] = cursor
    data = client.get("conversations.history", token=token, params=params)
    return data if data.get("ok") else {}


def _history_messages(data: RawAPIDict) -> list[RawAPIDict]:
    """The message dicts of a history page, or ``[]`` when the field is absent/malformed."""
    messages = data.get("messages")
    if not isinstance(messages, list):
        return []
    return [cast("RawAPIDict", message) for message in messages if isinstance(message, dict)]


def _walk_review_history(request: "SlackReviewSearchRequest") -> tuple[list["SlackReviewMatch"], bool]:
    """Walk a channel's recent history, matching PR URLs — the shared read core.

    The single implementation behind both :func:`search_review_permalinks` and
    :func:`read_recent_review_matches`; the two differ only in how they report
    read success. Returns ``(matches, read_ok)``: ``read_ok`` is ``False`` when a
    page came back not-ok (so a caller that needs the distinction can fail safe
    to suppression), ``True`` for a clean walk that simply found nothing.
    """
    if not request.token or not request.channel_id or not request.pr_urls:
        return [], True

    pr_url_set = set(request.pr_urls)
    matches: list[SlackReviewMatch] = []
    seen: set[str] = set()
    client = SlackHttpClient(timeout=request.timeout)
    workspace_domain = request.workspace_domain or _resolve_workspace_domain(client, request.token)
    ctx = _ChannelContext(
        channel_id=request.channel_id,
        channel_name=request.channel_name,
        workspace_domain=workspace_domain,
    )

    cursor: str | None = None
    for _ in range(request.max_pages):  # pragma: no branch
        data = _fetch_history_page(client, request.token, request.channel_id, cursor, request.oldest_ts)
        if not data:
            return matches, False
        for msg in _history_messages(data):
            matches.extend(_iter_review_matches(msg, pr_url_set, seen, ctx))
        if seen == pr_url_set or not data.get("has_more"):
            break
        cursor = next_cursor(data)
        if cursor is None:
            break
    return matches, True


@dataclass(frozen=True, slots=True)
class SlackReviewSearchRequest:
    token: str
    channel_id: str
    channel_name: str
    pr_urls: list[str]
    max_pages: int = 10
    workspace_domain: str = ""
    oldest_ts: str = ""
    timeout: float = 15.0


def search_review_permalinks(request: SlackReviewSearchRequest) -> list[SlackReviewMatch]:
    """Read recent messages from a Slack channel and match PR URLs.

    Uses conversations.history (no search:read scope needed).
    Matching is deterministic: exact PR URL substring match, no AI.
    """
    return _walk_review_history(request)[0]


@dataclass(frozen=True, slots=True)
class ReviewHistoryRead:
    """Outcome of a recency-bounded channel history read.

    ``ok`` distinguishes a clean read that simply found nothing
    (``ok=True, matches=[]`` → safe to post) from a read that could not
    complete (``ok=False`` → fail safe to NOT posting). ``search_review
    _permalinks`` deliberately collapses both into ``[]``; the dedup
    guard needs the distinction (#1084).
    """

    ok: bool
    matches: list[SlackReviewMatch]


def read_recent_review_matches(request: SlackReviewSearchRequest) -> ReviewHistoryRead:
    """Recency-bounded history read that reports read success explicitly.

    Mirrors :func:`search_review_permalinks` but returns
    :class:`ReviewHistoryRead` so the caller can tell "completed cleanly,
    nothing found" (``ok=True, matches=[]``) apart from "API said not-ok / page
    unavailable" (``ok=False``). A transport error still propagates — the guard
    wraps the call so a timeout/HTTP error fails safe to suppression.
    """
    matches, read_ok = _walk_review_history(request)
    return ReviewHistoryRead(ok=read_ok, matches=matches)


@dataclass(frozen=True, slots=True)
class SlackThreadActivityRequest:
    token: str
    channel_id: str
    thread_ts: str
    timeout: float = 15.0


@dataclass(frozen=True, slots=True)
class ThreadActivityRead:
    """Outcome of a single-thread ``conversations.replies`` read (#1084 follow-up).

    ``ok`` distinguishes a completed read from one that could not run
    (``ok=False`` ⇒ the guard fails safe to SUPPRESS). ``exists`` is False
    when the thread parent is gone (deleted). Slack carries no per-reaction
    timestamp, so ``has_reaction`` reports only presence — the caller treats
    a present reaction as fresh engagement.
    """

    ok: bool
    exists: bool
    parent_ts: str = ""
    latest_reply_ts: str = ""
    has_reaction: bool = False


def _ts_key(ts: str) -> float:
    try:
        return float(ts)
    except ValueError:
        return 0.0


# Slack ``conversations.replies`` errors that PROVE the thread/root is gone
# (deleted), as opposed to a transient read failure — see #3292.
_DELETION_ERRORS = frozenset({"thread_not_found", "message_not_found"})


def read_thread_activity(request: SlackThreadActivityRequest) -> ThreadActivityRead:
    """Read one thread's liveness + latest activity via ``conversations.replies``.

    Returns the thread parent's presence (``exists``), the parent ``ts``, the
    newest reply ``ts``, and whether the parent carries any reaction. An httpx
    transport error propagates so the caller can fail safe. A deletion API error
    (``thread_not_found`` / ``message_not_found``) and a ``subtype:"tombstone"``
    root are both proof of deletion → ``ok=True, exists=False``; every other
    ``ok:false`` body (ratelimited, auth) stays ``ok=False`` so the caller
    suppresses rather than re-posts on an uncertain read (#3292).
    """
    if not request.token or not request.channel_id or not request.thread_ts:
        return ThreadActivityRead(ok=True, exists=False)

    client = SlackHttpClient(timeout=request.timeout)
    data = client.get(
        "conversations.replies",
        token=request.token,
        params={"channel": request.channel_id, "ts": request.thread_ts, "limit": 200},
    )

    if not data.get("ok"):
        # A deleted root/thread comes back as an ``ok:false`` API error
        # (``thread_not_found`` / ``message_not_found``) — that is proof of
        # DELETION, not a read failure, so it must read as ``ok=True,
        # exists=False`` and let the reclaim → re-post branch fire (#3292).
        # Every other ``ok:false`` (ratelimited, auth) stays fail-safe:
        # ``ok=False`` ⇒ the guard suppresses rather than re-posting on doubt.
        if data.get("error") in _DELETION_ERRORS:
            return ThreadActivityRead(ok=True, exists=False)
        return ThreadActivityRead(ok=False, exists=False)
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return ThreadActivityRead(ok=True, exists=False)
    parent: RawAPIDict = cast("RawAPIDict", messages[0]) if isinstance(messages[0], dict) else {}
    # A tombstone root (parent deleted, replies survive) is gone — do not count
    # ``messages[0]`` as "exists" just because the array is non-empty (#3292).
    if parent.get("subtype") == "tombstone":
        return ThreadActivityRead(ok=True, exists=False)
    reply_dicts = [cast("RawAPIDict", m) for m in messages[1:] if isinstance(m, dict)]
    reply_tss = [str(m.get("ts", "")) for m in reply_dicts if m.get("ts")]
    return ThreadActivityRead(
        ok=True,
        exists=True,
        parent_ts=str(parent.get("ts", "")),
        latest_reply_ts=max(reply_tss, key=_ts_key, default=""),
        has_reaction=bool(parent.get("reactions")),
    )
