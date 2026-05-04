import re
from dataclasses import dataclass
from typing import cast

import httpx

from teatree.core.sync import RawAPIDict
from teatree.identity import agent_signature_suffix


def post_webhook_message(webhook_url: str, text: str, *, signature: str = "") -> RawAPIDict:
    """Post a Slack webhook message on the user's behalf.

    `signature` is appended only when `[teatree] agent_signature = true` in
    `~/.teatree.toml`. Default config keeps the message indistinguishable
    from one the user typed themselves — see `teatree.identity` and
    `skills/rules/SKILL.md` § "No AI Signature on Posts Made on the User's
    Behalf".
    """
    body = text + agent_signature_suffix(signature)
    response = httpx.post(webhook_url, json={"text": body}, timeout=10.0)
    response.raise_for_status()
    return cast("RawAPIDict", response.json())


@dataclass(frozen=True, slots=True)
class SlackReviewMatch:
    mr_url: str
    permalink: str
    channel: str


_MR_URL_RE = re.compile(r"https://[^\s|>]+/merge_requests/\d+")


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
    mr_url_set: set[str],
    seen: set[str],
    ctx: _ChannelContext,
) -> list[SlackReviewMatch]:
    """Extract SlackReviewMatch entries from a single Slack message."""
    text = str(msg.get("text", ""))
    ts = str(msg.get("ts", ""))
    if not ts:
        return []
    found_urls = _MR_URL_RE.findall(text)
    if not found_urls:
        return []

    permalink = f"https://{ctx.workspace_domain}/archives/{ctx.channel_id}/p{ts.replace('.', '')}"
    matches: list[SlackReviewMatch] = []
    for url in found_urls:
        clean_url = url.rstrip("/").split("#")[0]
        if clean_url in mr_url_set and clean_url not in seen:
            seen.add(clean_url)
            matches.append(SlackReviewMatch(mr_url=clean_url, permalink=permalink, channel=ctx.channel_name))
    return matches


def _fetch_history_page(
    client: httpx.Client,
    channel_id: str,
    cursor: str | None,
) -> RawAPIDict:
    """Fetch one page of conversations.history. Returns {} on non-ok response."""
    params: dict[str, str | int] = {"channel": channel_id, "limit": 100}
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
    mr_urls: list[str]
    max_pages: int = 10
    workspace_domain: str = ""


def search_review_permalinks(request: SlackReviewSearchRequest) -> list[SlackReviewMatch]:
    """Read recent messages from a Slack channel and match MR URLs.

    Uses conversations.history (no search:read scope needed).
    Matching is deterministic: exact MR URL substring match, no AI.
    """
    if not request.token or not request.channel_id or not request.mr_urls:
        return []

    mr_url_set = set(request.mr_urls)
    matches: list[SlackReviewMatch] = []
    seen: set[str] = set()

    with httpx.Client(headers={"Authorization": f"Bearer {request.token}"}, timeout=15.0) as client:
        workspace_domain = request.workspace_domain or _resolve_workspace_domain(client)
        ctx = _ChannelContext(
            channel_id=request.channel_id,
            channel_name=request.channel_name,
            workspace_domain=workspace_domain,
        )

        cursor: str | None = None
        for _ in range(request.max_pages):  # pragma: no branch
            data = _fetch_history_page(client, request.channel_id, cursor)
            if not data:
                break

            for msg in data.get("messages", []):  # ty: ignore[not-iterable]
                matches.extend(_iter_review_matches(msg, mr_url_set, seen, ctx))

            if seen == mr_url_set or not data.get("has_more"):
                break
            meta = data.get("response_metadata", {})
            cursor = meta.get("next_cursor") if isinstance(meta, dict) else None  # ty: ignore[invalid-argument-type]
            if not cursor:
                break

    return matches
