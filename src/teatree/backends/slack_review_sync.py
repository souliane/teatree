"""Slack review-permalink sync — attach review channel links to in-flight MRs."""

import logging

import httpx

from teatree.backends.slack import search_review_permalinks
from teatree.core.models import Ticket
from teatree.core.overlay_loader import get_overlay
from teatree.core.sync import SyncResult

logger = logging.getLogger(__name__)


def _collect_reviewable_mr_urls() -> tuple[list[str], dict[str, tuple[Ticket, str]]]:
    mr_urls: list[str] = []
    url_to_ticket: dict[str, tuple[Ticket, str]] = {}
    for ticket in Ticket.objects.in_flight():
        extra = ticket.extra if isinstance(ticket.extra, dict) else {}
        mrs = extra.get("mrs", {})
        if not isinstance(mrs, dict):
            continue
        for mr_url, mr in mrs.items():
            if not isinstance(mr, dict) or mr.get("draft") or mr.get("review_permalink"):
                continue
            clean_url = mr_url.rstrip("/").split("#")[0]
            mr_urls.append(clean_url)
            url_to_ticket[clean_url] = (ticket, mr_url)
    return mr_urls, url_to_ticket


def _apply_match(ticket: Ticket, mr_url: str, permalink: str, channel: str) -> bool:
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    mrs = extra.get("mrs", {})
    if not isinstance(mrs, dict):
        return False
    mr = mrs.get(mr_url)
    if not isinstance(mr, dict):
        return False
    mr["review_permalink"] = permalink
    mr["review_channel"] = channel
    extra["mrs"] = mrs
    ticket.extra = extra
    ticket.save(update_fields=["extra"])
    return True


def fetch_review_permalinks(result: SyncResult) -> None:
    overlay = get_overlay()
    token = overlay.config.get_slack_token()
    channel_name, channel_id = overlay.config.get_review_channel()
    if not token or not channel_id:
        return

    mr_urls, url_to_ticket = _collect_reviewable_mr_urls()
    if not mr_urls:
        return

    try:
        matches = search_review_permalinks(
            token=token,
            channel_id=channel_id,
            channel_name=channel_name,
            mr_urls=mr_urls,
        )
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        result.errors.append(f"Slack review sync: {exc}")
        return

    for match in matches:
        ticket, mr_url = url_to_ticket[match.mr_url]
        if _apply_match(ticket, mr_url, match.permalink, match.channel):
            result.reviews_synced += 1
