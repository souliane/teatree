"""Pause verdict for a review re-ask."""

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models import ReviewRequestPost
from teatree.core.review.review_pause import PauseState, read_pause_state
from teatree.loop.scanners.base import ScanSignal
from teatree.types import RawAPIDict


def pause_skip(
    post: ReviewRequestPost,
    messaging: MessagingBackend,
    *,
    visible_root: RawAPIDict | None = None,
) -> ScanSignal | None:
    state = read_pause_state(post, messaging, visible_root=visible_root)
    if state is PauseState.NOT_PAUSED:
        return None
    return ScanSignal(
        kind="review_nag.mr_paused",
        summary=f"Skipping nag for {post.mr_url} — pause state is {state.value}",
        payload={"mr_url": post.mr_url, "post_id": post.pk, "pause_state": state.value},
    )
