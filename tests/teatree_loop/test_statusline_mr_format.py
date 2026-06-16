"""Tests for the bare ``!N`` / ``#N`` chip rendering (#1377).

Per the binding spec the chip is just the number — no per-MR title
chunk, no annotation, no review-permalink suffix. Earlier shapes
(``!N (title)``, ``!N (title) (1 notes)``) are gone.
"""

import pytest

from teatree.core.models.ticket import Ticket
from teatree.loop.dispatch import DispatchAction
from teatree.loop.rendering import zones_for


def _pr_action(
    *, url: str, iid: int, title: str = "", zone: str = "in_flight", overlay: str = "teatree"
) -> DispatchAction:
    return DispatchAction(
        kind="statusline",
        zone=zone,
        detail=f"PR #{iid} {title}",
        payload={"url": url, "iid": iid, "title": title, "overlay": overlay},
    )


def _render_blob(actions: list[DispatchAction]) -> str:
    zones = zones_for(actions, colorize=False)
    return "\n".join(
        item if isinstance(item, str) else item.text
        for zone in (zones.anchors, zones.action_needed, zones.in_flight)
        for item in zone
    )


class TestMrChipIsBare:
    def test_chip_is_bare_iid_no_title_chunk(self) -> None:
        actions = [
            _pr_action(
                url="https://example.com/p/1/merge_requests/123",
                iid=123,
                title="feat(loop): add multi-loop anchors",
            ),
        ]
        blob = _render_blob(actions)

        assert "!123" in blob, repr(blob)
        # Per #1377 the title chunk is removed from the chip — no decoration.
        assert "(add multi-loop anchors)" not in blob, repr(blob)
        assert "feat(loop):" not in blob, repr(blob)

    def test_multiple_open_mrs_space_separated(self) -> None:
        actions = [
            _pr_action(url="https://example.com/p/1/merge_requests/100", iid=100, title="alpha"),
            _pr_action(url="https://example.com/p/1/merge_requests/101", iid=101, title="beta"),
        ]
        blob = _render_blob(actions)

        assert "!100" in blob, repr(blob)
        assert "!101" in blob, repr(blob)
        # Comma-joining stayed gone; chips space-separated.
        assert ", !101" not in blob, repr(blob)
        # Per #1377 no per-MR title chunks.
        assert "(alpha)" not in blob, repr(blob)
        assert "(beta)" not in blob, repr(blob)

    def test_draft_chip_has_no_annotation(self) -> None:
        """A draft MR's ``(N notes)`` annotation is gone per #1377."""
        action = DispatchAction(
            kind="statusline",
            zone="in_flight",
            detail="PR #200",
            payload={
                "url": "https://example.com/p/1/merge_requests/200",
                "iid": 200,
                "title": "wip: experimental",
                "overlay": "teatree",
                "draft_count": 1,
            },
        )
        blob = _render_blob([action])

        assert "!200" in blob, repr(blob)
        # Annotation chunk and title chunk both gone per #1377.
        assert "(experimental)" not in blob, repr(blob)
        assert "(1 notes)" not in blob, repr(blob)


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestMergedMrsNotShown:
    """``build_review_post_permalinks`` filters out MERGED PullRequests (Culprit A)."""

    def test_merged_mrs_not_shown(self) -> None:
        from teatree.core.models.pull_request import PullRequest  # noqa: PLC0415
        from teatree.core.models.review_request_post import ReviewRequestPost  # noqa: PLC0415

        ticket = Ticket.objects.create(
            overlay="teatree",
            issue_url="https://example.com/issues/9",
            state=Ticket.State.MERGED,
        )
        merged_pr_url = "https://example.com/p/1/merge_requests/999"
        # Use bound transition to advance the PR to MERGED state.
        pr = PullRequest.objects.create(
            ticket=ticket,
            overlay="teatree",
            url=merged_pr_url,
            repo="teatree",
            iid="999",
        )
        pr.mark_merged()
        pr.save()

        ReviewRequestPost.objects.create(
            mr_url=merged_pr_url,
            slack_channel_id="C9",
            slack_thread_ts="1779.0001",
        )

        from teatree.loop.rendering_permalinks import build_review_post_permalinks  # noqa: PLC0415

        actions = [_pr_action(url=merged_pr_url, iid=999, title="old MR")]

        permalinks = build_review_post_permalinks(actions)

        # A merged PR must NOT produce a Slack permalink — stale rows 404.
        assert merged_pr_url not in permalinks, permalinks


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestTracker404StripsClickableUrl:
    """A tracker_404 ticket renders without a clickable URL (Culprit B)."""

    def test_tracker_404_strips_clickable_url(self) -> None:
        from teatree.loop.scanners.active_tickets import ActiveTicketsScanner  # noqa: PLC0415

        Ticket.objects.create(
            overlay="teatree",
            issue_url="https://example.com/issues/404",
            state=Ticket.State.STARTED,
            short_description="orphaned tracker",
            extra={"tracker_404": True},
        )

        signals = ActiveTicketsScanner(overlay_name="teatree").scan()

        assert len(signals) == 1
        # ``issue_url`` must be blanked so the renderer emits a bare ``#N``.
        assert signals[0].payload.get("issue_url") == "", signals[0].payload
