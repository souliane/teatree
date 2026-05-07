"""Slack review-permalink sync — attach review channel links to in-flight PRs."""

import logging

import httpx

from teatree.backends.slack import SlackReviewSearchRequest, search_review_permalinks
from teatree.core.models import Ticket
from teatree.core.overlay_loader import get_overlay
from teatree.core.sync import SyncResult

logger = logging.getLogger(__name__)


def _collect_reviewable_pr_urls() -> tuple[list[str], dict[str, tuple[Ticket, str]]]:
    pr_urls: list[str] = []
    url_to_ticket: dict[str, tuple[Ticket, str]] = {}
    for ticket in Ticket.objects.in_flight():
        extra = ticket.extra if isinstance(ticket.extra, dict) else {}
        prs = extra.get("prs", {})
        if not isinstance(prs, dict):
            continue
        for pr_url, pr in prs.items():
            if not isinstance(pr, dict) or pr.get("draft") or pr.get("review_permalink"):
                continue
            clean_url = pr_url.rstrip("/").split("#")[0]
            pr_urls.append(clean_url)
            url_to_ticket[clean_url] = (ticket, pr_url)
    return pr_urls, url_to_ticket


def _apply_match(ticket: Ticket, pr_url: str, permalink: str, channel: str) -> bool:
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    prs = extra.get("prs", {})
    if not isinstance(prs, dict):
        return False
    pr = prs.get(pr_url)
    if not isinstance(pr, dict):
        return False
    pr["review_permalink"] = permalink
    pr["review_channel"] = channel
    extra["prs"] = prs
    ticket.extra = extra
    ticket.save(update_fields=["extra"])
    return True


def fetch_review_permalinks(result: SyncResult) -> None:
    overlay = get_overlay()
    token = overlay.config.get_slack_token()
    channel_name, channel_id = overlay.config.get_review_channel()
    if not token or not channel_id:
        return

    pr_urls, url_to_ticket = _collect_reviewable_pr_urls()
    if not pr_urls:
        return

    try:
        matches = search_review_permalinks(
            SlackReviewSearchRequest(
                token=token,
                channel_id=channel_id,
                channel_name=channel_name,
                pr_urls=pr_urls,
            )
        )
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        result.errors.append(f"Slack review sync: {exc}")
        return

    for match in matches:
        ticket, pr_url = url_to_ticket[match.pr_url]
        if _apply_match(ticket, pr_url, match.permalink, match.channel):
            result.reviews_synced += 1
