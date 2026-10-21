"""Atomic claim and reply reconciliation for a review-request batch thread."""

import datetime as dt
from dataclasses import dataclass

from django.db import transaction

from teatree.core.models import ReviewRequestPost


class ReviewNagPostReconciliationError(RuntimeError):
    """Slack reported success without a reply coordinate for the shared claim."""


@dataclass(frozen=True, slots=True)
class ReviewNagClaim:
    previous: tuple[tuple[int, dt.datetime | None, int], ...]
    posts: tuple[ReviewRequestPost, ...]
    claimed_at: dt.datetime
    nag_count: int

    @classmethod
    def take(cls, post: ReviewRequestPost, right_now: dt.datetime, interval: dt.timedelta) -> "ReviewNagClaim | None":
        with transaction.atomic():
            rows = ReviewRequestPost.objects.open_thread(post).select_for_update()
            peers = list(rows.order_by("pk"))
            if not any(peer.pk == post.pk for peer in peers):
                return None
            if rows.nagged_within(right_now, interval):
                return None
            previous = tuple((peer.pk, peer.last_nag_at, peer.nag_count) for peer in peers)
            count = max(peer.nag_count for peer in peers) + 1
            ReviewRequestPost.objects.filter(pk__in=[peer.pk for peer in peers]).update(
                last_nag_at=right_now, nag_count=count
            )
        return cls(previous=previous, posts=tuple(peers), claimed_at=right_now, nag_count=count)

    def release(self) -> None:
        with transaction.atomic():
            for post_id, previous_at, previous_count in self.previous:
                ReviewRequestPost.objects.filter(
                    pk=post_id,
                    last_nag_at=self.claimed_at,
                    nag_count=self.nag_count,
                ).update(last_nag_at=previous_at, nag_count=previous_count)

    def persist_reply_ts(self, reply_ts: str) -> None:
        with transaction.atomic():
            updated = ReviewRequestPost.objects.filter(
                pk__in=[post_id for post_id, _, _ in self.previous],
                last_nag_at=self.claimed_at,
                nag_count=self.nag_count,
            ).update(last_nag_at=self.claimed_at, nag_count=self.nag_count, last_nag_reply_ts=reply_ts)
            if updated != len(self.previous):
                msg = "review nag claim could not persist every reply timestamp"
                raise ReviewNagPostReconciliationError(msg)
