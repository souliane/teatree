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
    """Refinement 2 (pre-#1377): rich state coverage included ``in_review`` and ``not_started``.

    #1377 reverses that decision — the anchor row is now strictly the
    actively-shipping slice (``not_started`` and ``in_review`` moved into
    ``_NOISE_STATES``). The new contract is pinned in
    ``test_statusline_terse_format``; this class now pins the inverse.
    """

    def test_in_review_state_filtered_out(self, tmp_path: Path) -> None:
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
        assert "in_review" not in body
        assert "#100" not in body

    def test_not_started_state_filtered_out(self, tmp_path: Path) -> None:
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
        assert "#200" not in body

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

    def test_noise_states_includes_in_review_and_not_started(self) -> None:
        # #1377 moved both states into the noise set so the anchor row stays
        # the actively-shipping slice. Inverse of the pre-#1377 contract.
        assert "in_review" in _NOISE_STATES
        assert "not_started" in _NOISE_STATES


class TestItemFormatDescriptionAlwaysShown:
    """Refinement 3: ``#N (desc) (!MR)`` carries the description chunk."""

    def test_description_chunk_when_title_present(self, tmp_path: Path) -> None:
        action = _statusline_action(
            {
                "overlay": "overlay-a",
                "ticket_number": "500",
                "state": "started",
                "issue_url": "https://example.com/tracker/500",
                "title": "add new scanner guard",
            },
        )
        zones = zones_for([action], colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        assert "#500" in body
        # Terse topic keeps the first three words of the title.
        assert "(add new scanner)" in body


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
    """Refinement 5 (revised): one per-loop-named summary line for live loops.

    The original refinement returned one line per live loop, then a later
    refit collapsed to a single ``… · N loops live`` count. The user asked
    instead for one line that names each live loop with its next tick; this
    locks that shape (the bare count is gone).
    """

    def test_returns_one_line_naming_each_loop(self) -> None:
        # t3-master is excluded from the shared line — its badge is
        # per-session in statusline.sh. The other two loops appear.
        leases = [
            ("loop-tick", None),
            ("loop-slack-answer", None),
            ("t3-master", None),
        ]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
        ):
            lines = live_loops_anchor()
        assert len(lines) == 1
        assert lines[0].startswith("tick")
        assert "loop running" not in lines[0]
        assert "loops live" not in lines[0]
        assert "tick" in lines[0]
        assert "slack-answer" in lines[0]
        # t3-master excluded from the shared line (per-session badge in sh).
        assert "owner" not in lines[0]

    def test_no_live_leases_returns_empty(self) -> None:
        # Empty list → no lines (no statusline noise when no loop is live).
        with patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[]):
            assert live_loops_anchor() == []

    def test_fails_open_on_query_error(self) -> None:
        # When the underlying DB read raises, callers see [] — a broken
        # LoopLease query must never blank the statusline.
        with patch("teatree.loop.statusline_loops._live_loop_leases", side_effect=RuntimeError("db down")):
            assert live_loops_anchor() == []


class TestZonesForIntegratesLoopsAnchor:
    """``zones_for`` surfaces the consolidated loop summary in the anchors zone."""

    def test_zones_for_appends_consolidated_loop_line(self, tmp_path: Path) -> None:
        # t3-master lease present but excluded from the shared line
        # (per-session badge in statusline.sh replaces it).
        with (
            patch(
                "teatree.loop.statusline_loops._live_loop_leases",
                return_value=[("loop-tick", None), ("t3-master", None)],
            ),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
        ):
            zones: StatuslineZones = zones_for([], colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        assert "loop running" not in body
        assert "loops live" not in body
        assert "tick" in body
        # t3-master absent from the shared zones file.
        assert "owner" not in body
        # Per-loop dump tokens absent.
        assert "loop:tick" not in body
        assert "loop:owner" not in body
