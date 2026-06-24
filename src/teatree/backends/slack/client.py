from dataclasses import dataclass
from typing import cast

import httpx

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


def _resolve_workspace_domain(client: httpx.Client) -> str:
    """Resolve Slack workspace domain via auth.test API."""
    auth_resp = client.get("https://slack.com/api/auth.test")
    auth_data = auth_resp.json() if auth_resp.is_success else {}
    url = str(auth_data.get("url", "https://app.slack.com/")).rstrip("/")
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
    client: httpx.Client,
    channel_id: str,
    cursor: str | None,
    oldest_ts: str = "",
) -> RawAPIDict:
    """Fetch one page of conversations.history. Returns {} on non-ok response.

    ``oldest_ts`` bounds the read to messages at or after that Slack ``ts``
    so a recency-windowed dedup never paginates the entire channel history.
    """
    params: dict[str, str | int] = {"channel": channel_id, "limit": 100}
    if oldest_ts:
        params["oldest"] = oldest_ts
    if cursor:
        params["cursor"] = cursor
    response = client.get("https://slack.com/api/conversations.history", params=params)
    response.raise_for_status()
    data = response.json()
    return data if data.get("ok") else {}


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
    if not request.token or not request.channel_id or not request.pr_urls:
        return []

    pr_url_set = set(request.pr_urls)
    matches: list[SlackReviewMatch] = []
    seen: set[str] = set()

    with httpx.Client(headers={"Authorization": f"Bearer {request.token}"}, timeout=request.timeout) as client:
        workspace_domain = request.workspace_domain or _resolve_workspace_domain(client)
        ctx = _ChannelContext(
            channel_id=request.channel_id,
            channel_name=request.channel_name,
            workspace_domain=workspace_domain,
        )

        cursor: str | None = None
        for _ in range(request.max_pages):  # pragma: no branch
            data = _fetch_history_page(client, request.channel_id, cursor, request.oldest_ts)
            if not data:
                break

            for msg in data.get("messages", []):  # ty: ignore[not-iterable]
                matches.extend(_iter_review_matches(msg, pr_url_set, seen, ctx))

            if seen == pr_url_set or not data.get("has_more"):
                break
            meta = data.get("response_metadata", {})
            cursor = meta.get("next_cursor") if isinstance(meta, dict) else None  # ty: ignore[invalid-argument-type]
            if not cursor:
                break

    return matches


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
    nothing found" apart from "API said not-ok / page unavailable". An
    httpx transport error still propagates — the guard wraps the call so
    a timeout/HTTP error fails safe to suppression.
    """
    if not request.token or not request.channel_id or not request.pr_urls:
        return ReviewHistoryRead(ok=True, matches=[])

    pr_url_set = set(request.pr_urls)
    matches: list[SlackReviewMatch] = []
    seen: set[str] = set()
    read_ok = True

    with httpx.Client(headers={"Authorization": f"Bearer {request.token}"}, timeout=request.timeout) as client:
        workspace_domain = request.workspace_domain or _resolve_workspace_domain(client)
        ctx = _ChannelContext(
            channel_id=request.channel_id,
            channel_name=request.channel_name,
            workspace_domain=workspace_domain,
        )

        cursor: str | None = None
        for _ in range(request.max_pages):  # pragma: no branch
            data = _fetch_history_page(client, request.channel_id, cursor, request.oldest_ts)
            if not data:
                read_ok = False
                break

            for msg in data.get("messages", []):  # ty: ignore[not-iterable]
                matches.extend(_iter_review_matches(msg, pr_url_set, seen, ctx))

            if seen == pr_url_set or not data.get("has_more"):
                break
            meta = data.get("response_metadata", {})
            cursor = meta.get("next_cursor") if isinstance(meta, dict) else None  # ty: ignore[invalid-argument-type]
            if not cursor:
                break

    return ReviewHistoryRead(ok=read_ok, matches=matches)
