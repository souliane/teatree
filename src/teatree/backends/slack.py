import re
from dataclasses import dataclass
from typing import cast

import httpx


def post_webhook_message(webhook_url: str, text: str) -> dict[str, object]:
    response = httpx.post(webhook_url, json={"text": text}, timeout=10.0)
    response.raise_for_status()
    return cast("dict[str, object]", response.json())


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


def search_review_permalinks(  # noqa: C901, PLR0912, PLR0913
    *,
    token: str,
    channel_id: str,
    channel_name: str,
    mr_urls: list[str],
    max_pages: int = 10,
    workspace_domain: str = "",
) -> list[SlackReviewMatch]:
    """Read recent messages from a Slack channel and match MR URLs.

    Uses conversations.history (no search:read scope needed).
    Matching is deterministic: exact MR URL substring match, no AI.
    """
    if not token or not channel_id or not mr_urls:
        return []

    mr_url_set = set(mr_urls)
    matches: list[SlackReviewMatch] = []
    seen: set[str] = set()

    with httpx.Client(
        headers={"Authorization": f"Bearer {token}"},
        timeout=15.0,
    ) as client:
        if not workspace_domain:
            workspace_domain = _resolve_workspace_domain(client)

        cursor = None
        for _ in range(max_pages):  # pragma: no branch
            params: dict[str, str | int] = {"channel": channel_id, "limit": 100}
            if cursor:
                params["cursor"] = cursor

            response = client.get(
                "https://slack.com/api/conversations.history",
                params=params,
            )
            response.raise_for_status()
            data = response.json()

            if not data.get("ok"):
                break

            for msg in data.get("messages", []):
                text = str(msg.get("text", ""))
                ts = str(msg.get("ts", ""))
                if not ts:
                    continue

                found_urls = _MR_URL_RE.findall(text)
                if not found_urls:
                    continue

                # Build permalink: https://workspace.slack.com/archives/CHANNEL/pTIMESTAMP
                ts_clean = ts.replace(".", "")
                permalink = f"https://{workspace_domain}/archives/{channel_id}/p{ts_clean}"

                for url in found_urls:
                    clean_url = url.rstrip("/").split("#")[0]
                    if clean_url in mr_url_set and clean_url not in seen:
                        seen.add(clean_url)
                        matches.append(
                            SlackReviewMatch(
                                mr_url=clean_url,
                                permalink=permalink,
                                channel=channel_name,
                            ),
                        )

            if seen == mr_url_set:
                break
            if not data.get("has_more"):
                break  # pragma: no cover — always exits via seen==mr_url_set or no cursor
            meta = data.get("response_metadata", {})
            cursor = meta.get("next_cursor")
            if not cursor:
                break

    return matches
