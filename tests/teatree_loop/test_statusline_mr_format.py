"""Tests for the ``!N (MR title)`` rendering (#1156).

Pre-#1156: bare ``!1234`` ref, comma-joined when multiple.
Post-#1156: ``!1234 (MR title)`` ref, space-separated when multiple.
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


class TestMrLineIncludesTitle:
    def test_mr_line_includes_title(self) -> None:
        actions = [
            _pr_action(
                url="https://example.com/p/1/merge_requests/123",
                iid=123,
                title="feat(loop): add multi-loop anchors",
            ),
        ]
        blob = _render_blob(actions)

        # The MR ref now carries its title — under NO_COLOR the rendered
        # form is ``!N <url> (title)`` because the iid is wrapped in the
        # plain-text URL fallback.
        assert "!123" in blob, repr(blob)
        assert "(feat(loop): add multi-loop anchors)" in blob, repr(blob)
        # The title chunk must come *after* the iid.
        assert blob.index("!123") < blob.index("(feat(loop): add multi-loop anchors)"), repr(blob)

    def test_multiple_open_mrs_space_separated(self) -> None:
        actions = [
            _pr_action(url="https://example.com/p/1/merge_requests/100", iid=100, title="alpha"),
            _pr_action(url="https://example.com/p/1/merge_requests/101", iid=101, title="beta"),
        ]
        blob = _render_blob(actions)

        # Both MRs render with their title. They are joined by a single
        # space, never ``, `` (comma-joining was the pre-#1156 form).
        assert "(alpha) !101" in blob, repr(blob)
        # Comma-joining must be gone (no ``, !101``).
        assert ", !101" not in blob, repr(blob)

    def test_drafts_included(self) -> None:
        """A draft MR (annotation present) still renders with its title."""
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

        # Title appears alongside the existing annotation.
        assert "!200" in blob, repr(blob)
        assert "wip: experimental" in blob, repr(blob)


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
