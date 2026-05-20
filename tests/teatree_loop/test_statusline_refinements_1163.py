"""Statusline refinements per #1163.

Five refinements covered: dedup user identities across overlays, rich
state coverage (in_review / not_started surface), item format ``#N
(desc) (!MR)``, no 404 links, and multi-loop anchors (one line per
live LoopLease row).
"""

from pathlib import Path
from unittest.mock import patch

from teatree.loop.dispatch import DispatchAction
from teatree.loop.rendering import zones_for
from teatree.loop.rendering_classification import _classify_actions
from teatree.loop.rendering_zones import _NOISE_STATES
from teatree.loop.statusline import StatuslineZones, live_loops_anchor, render


def _statusline_action(spec: dict[str, str | bool]) -> DispatchAction:
    """Build the canonical ``ticket.active`` statusline action.

    Spec keys: ``overlay``, ``ticket_number``, ``state``, ``issue_url``,
    optional ``title`` and ``tracker_404``. Single-dict signature keeps
    the helper under the per-function arg ceiling.
    """
    ticket_number = str(spec["ticket_number"])
    state = str(spec["state"])
    payload: dict[str, object] = {
        "ticket_number": ticket_number,
        "state": state,
        "issue_url": spec["issue_url"],
        "title": spec.get("title", ""),
        "overlay": spec["overlay"],
    }
    if spec.get("tracker_404"):
        payload["tracker_404"] = True
    return DispatchAction(
        kind="statusline",
        zone="anchors",
        detail=f"#{ticket_number} {state}",
        payload=payload,
    )


class TestRichStateCoverage:
    """Refinement 2: rich state coverage (drop only terminal states)."""

    def test_in_review_state_surfaces(self, tmp_path: Path) -> None:
        action = _statusline_action(
            {
                "overlay": "overlay-a",
                "ticket_number": "100",
                "state": "in_review",
                "issue_url": "https://example.com/tracker/100",
                "title": "review me",
            },
        )
        zones = zones_for([action], colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        assert "in_review:" in body or "in_review" in body
        assert "#100" in body

    def test_not_started_state_surfaces(self, tmp_path: Path) -> None:
        action = _statusline_action(
            {
                "overlay": "overlay-a",
                "ticket_number": "200",
                "state": "not_started",
                "issue_url": "https://example.com/tracker/200",
                "title": "not started yet",
            },
        )
        zones = zones_for([action], colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        assert "#200" in body

    def test_terminal_states_still_filtered(self, tmp_path: Path) -> None:
        # ``delivered`` / ``merged`` / ``shipped`` / ``retrospected`` /
        # ``closed`` remain noise — nothing actionable to show.
        actions = [
            _statusline_action(
                {
                    "overlay": "overlay-a",
                    "ticket_number": str(300 + idx),
                    "state": state,
                    "issue_url": f"https://example.com/tracker/{300 + idx}",
                    "title": f"terminal {state}",
                },
            )
            for idx, state in enumerate(["delivered", "merged", "shipped", "retrospected", "closed"])
        ]
        zones = zones_for(actions, colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        for n in range(300, 305):
            assert f"#{n}" not in body

    def test_noise_states_no_longer_includes_in_review_or_not_started(self) -> None:
        # The constant declares the renderer's filter — pin its contents so
        # we don't silently regress to dropping rich states again.
        assert "in_review" not in _NOISE_STATES
        assert "not_started" not in _NOISE_STATES


class TestItemFormatDescriptionAlwaysShown:
    """Refinement 3: ``#N (desc) (!MR)`` carries the description chunk."""

    def test_description_chunk_when_title_present(self, tmp_path: Path) -> None:
        action = _statusline_action(
            {
                "overlay": "overlay-a",
                "ticket_number": "500",
                "state": "started",
                "issue_url": "https://example.com/tracker/500",
                "title": "add new docgen guard",
            },
        )
        zones = zones_for([action], colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        assert "#500" in body
        assert "(add new docgen guard)" in body


class TestNo404Links:
    """Refinement 4: drop URL for tracker-404 tickets."""

    def test_tracker_404_ticket_renders_bare_number_no_url(self, tmp_path: Path) -> None:
        action = _statusline_action(
            {
                "overlay": "overlay-a",
                "ticket_number": "214",
                "state": "started",
                "issue_url": "https://example.com/tracker/214",
                "title": "deleted ticket",
                "tracker_404": True,
            },
        )
        zones = zones_for([action], colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        # ``#214`` must still surface as a bare label so the user knows the
        # local FSM row is dangling, but the dead URL must NOT be linked.
        assert "#214" in body
        assert "https://example.com/tracker/214" not in body

    def test_classifier_drops_url_when_tracker_404(self) -> None:
        action = _statusline_action(
            {
                "overlay": "overlay-a",
                "ticket_number": "214",
                "state": "started",
                "issue_url": "https://example.com/tracker/214",
                "title": "deleted",
                "tracker_404": True,
            },
        )
        c = _classify_actions([action])
        tickets = c.active_tickets["overlay-a"]
        assert len(tickets) == 1
        # tuple shape: (ticket_number, state, issue_url, title)
        _, _, issue_url, _ = tickets[0]
        assert issue_url == ""


class TestDedupAcrossOverlays:
    """Refinement 1: dedup the same logical ticket across overlays."""

    def test_same_issue_url_under_multiple_overlays_renders_once(self, tmp_path: Path) -> None:
        # Both ``overlay-a`` and ``overlay-b`` observe the same underlying
        # tracker row (#8446 / same issue_url). The renderer surfaces it
        # under a single overlay (first occurrence wins), not twice.
        shared_url = "https://example.com/tracker/8446"
        action_a = _statusline_action(
            {
                "overlay": "overlay-a",
                "ticket_number": "8446",
                "state": "started",
                "issue_url": shared_url,
                "title": "shared ticket",
            },
        )
        action_b = _statusline_action(
            {
                "overlay": "overlay-b",
                "ticket_number": "8446",
                "state": "started",
                "issue_url": shared_url,
                "title": "shared ticket",
            },
        )
        zones = zones_for([action_a, action_b], colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        # One #8446 row, not two — the second overlay's duplicate suppressed.
        assert body.count("#8446") == 1


class TestLiveLoopsAnchor:
    """Refinement 5: one anchor line per live loop."""

    def test_one_anchor_line_per_live_lease(self) -> None:
        leases = [
            ("loop-tick", "sessA"),
            ("loop-slack-answer", "sessB"),
            ("loop-owner", "sessA"),
        ]
        with patch("teatree.loop.statusline._live_loop_names", return_value=leases):
            lines = live_loops_anchor()
        assert len(lines) == 3
        assert any("loop:tick" in line for line in lines)
        assert any("loop:slack-answer" in line for line in lines)
        assert any("loop:owner" in line for line in lines)

    def test_no_live_leases_returns_empty(self) -> None:
        # Empty list → no lines (no statusline noise when no loop is live).
        with patch("teatree.loop.statusline._live_loop_names", return_value=[]):
            assert live_loops_anchor() == []

    def test_fails_open_on_query_error(self) -> None:
        # When the underlying DB read raises, callers see [] — a broken
        # LoopLease query must never blank the statusline.
        with patch("teatree.loop.statusline._live_loop_names", side_effect=RuntimeError("db down")):
            assert live_loops_anchor() == []


class TestZonesForIntegratesLoopsAnchor:
    """``zones_for`` surfaces live loops in the anchors zone."""

    def test_zones_for_appends_live_loops(self, tmp_path: Path) -> None:
        with patch(
            "teatree.loop.statusline._live_loop_names",
            return_value=[("loop-tick", "sessA"), ("loop-owner", "sessA")],
        ):
            zones: StatuslineZones = zones_for([], colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        assert "loop:tick" in body
        assert "loop:owner" in body
