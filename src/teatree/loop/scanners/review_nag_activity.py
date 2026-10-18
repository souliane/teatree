"""Find the last engagement in a review-request thread."""

import datetime as dt

from teatree.core.models import ReviewRequestPost
from teatree.types import RawAPIDict


def _epoch(ts: str) -> float:
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


def _requester_only(reaction: object, requester_slack_id: str) -> bool:
    if not requester_slack_id or not isinstance(reaction, dict):
        return False
    users = reaction.get("users")
    if not isinstance(users, list) or not users:
        return False
    if not all(user == requester_slack_id for user in users):
        return False
    if "count" not in reaction:
        return True
    count = reaction["count"]
    # Slack can omit users from a reaction when the count exceeds the returned list.
    return type(count) is int and count <= len(users)


def _engaged_by_someone_else(reactions: object, requester_slack_id: str) -> bool:
    if not reactions:
        return False
    if not isinstance(reactions, list):
        return True
    return any(not _requester_only(reaction, requester_slack_id) for reaction in reactions)


def thread_activity(
    post: ReviewRequestPost,
    replies: list[RawAPIDict],
    right_now: dt.datetime,
    *,
    requester_slack_id: str,
) -> dt.datetime:
    epochs = [_epoch(post.slack_thread_ts)]
    for msg in replies:
        if not isinstance(msg, dict):
            continue
        if _engaged_by_someone_else(msg.get("reactions"), requester_slack_id):
            return right_now
        ts = msg.get("ts")
        if isinstance(ts, str):
            epochs.append(_epoch(ts))
    latest = max((epoch for epoch in epochs if epoch > 0.0), default=0.0)
    if latest <= 0.0:
        return post.created_at
    return dt.datetime.fromtimestamp(latest, tz=dt.UTC)
