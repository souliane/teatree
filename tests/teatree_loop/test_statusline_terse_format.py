"""Statusline terse-format anchor row (#1377 item shape, #130 state labels).

The anchor "what am I working on" line for an overlay stays terse:
``not_started`` and ``in_review`` are filtered out — the in-flight zone
surfaces in-review work via PR/MR chips, and the not_started backlog is
not user-actionable from the statusline.

The per-item shape is the terse #1377 form ``#N (2-3 word topic !MR1
!MR2 …)``. #130 restores the FSM ``state:`` group label that #1377 had
dropped, so the line reads ``[overlay] started: #N (topic !chips)`` —
grouping tickets by status was the user's explicit, latest requirement.
"""

import re
from pathlib import Path

from teatree.loop.dispatch import DispatchAction
from teatree.loop.rendering import zones_for
from teatree.loop.rendering_zones import _NOISE_STATES
from teatree.loop.statusline import render


def _ticket_action(num: str, state: str, *, overlay: str = "acme", title: str = "") -> DispatchAction:
    return DispatchAction(
        kind="statusline",
        zone="anchors",
        detail=f"#{num} {state}",
        payload={
            "ticket_number": num,
            "state": state,
            "overlay": overlay,
            "issue_url": f"https://example.com/issues/{num}",
            "title": title,
        },
    )


class TestNotStartedDroppedFromAnchor:
    """``not_started`` is a backlog state; the anchor line is not the place."""

    def test_not_started_state_filtered_out_of_anchor(self, tmp_path: Path) -> None:
        zones = zones_for(
            [
                _ticket_action("1", "not_started", overlay="ov"),
                _ticket_action("5", "not_started", overlay="ov"),
                _ticket_action("6", "not_started", overlay="ov"),
            ],
            colorize=False,
        )
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        assert "#1" not in body, repr(body)
        assert "#5" not in body, repr(body)
        assert "#6" not in body, repr(body)
        assert "not_started" not in body, repr(body)

    def test_not_started_in_noise_states(self) -> None:
        assert "not_started" in _NOISE_STATES


class TestInReviewDroppedFromAnchor:
    """``in_review`` work surfaces via PR/MR chips, not the anchor row."""

    def test_in_review_state_filtered_out_of_anchor(self, tmp_path: Path) -> None:
        zones = zones_for(
            [_ticket_action("100", "in_review", overlay="ov", title="some review")],
            colorize=False,
        )
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        assert "#100" not in body, repr(body)
        assert "in_review" not in body, repr(body)

    def test_in_review_in_noise_states(self) -> None:
        assert "in_review" in _NOISE_STATES


class TestStartedStateRendersStateLabelledCanonicalShape:
    """The FSM ``state:`` group label prefixes the terse canonical item (#130)."""

    def test_started_anchor_has_state_prefix(self, tmp_path: Path) -> None:
        zones = zones_for(
            [_ticket_action("8495", "started", overlay="acme", title="widget margin")],
            colorize=False,
        )
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        assert "started:" in body, repr(body)
        assert "#8495" in body, repr(body)
        assert "(widget margin)" in body, repr(body)

    def test_anchor_line_matches_state_labelled_format_regex(self, tmp_path: Path) -> None:
        zones = zones_for(
            [_ticket_action("8495", "started", overlay="acme", title="widget margin")],
            colorize=True,
        )
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=True)
        body = target.read_text()
        # Strip ANSI CSI escapes and OSC-8 hyperlink markers to recover the
        # visible glyphs the user actually sees in their terminal.
        visible = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", body)
        visible = re.sub(r"\x1b\]8;[^\x07\x1b]*(?:\x1b\\|\x07)", "", visible)
        anchor_lines = [line for line in visible.splitlines() if line.startswith("[acme]") and "#" in line]
        assert anchor_lines, repr(visible)
        pattern = re.compile(r"^\[[^\]]+\] started: #\d+ \(.+\)$")
        for line in anchor_lines:
            assert pattern.match(line), f"line {line!r} does not match state-labelled terse format"


class TestOnlyOneAnchorLinePerOverlay:
    """Multiple ``started`` tickets in one overlay still collapse to one line."""

    def test_multiple_started_tickets_render_one_line_per_overlay(self, tmp_path: Path) -> None:
        zones = zones_for(
            [
                _ticket_action("100", "started", overlay="acme", title="alpha"),
                _ticket_action("200", "started", overlay="acme", title="beta"),
            ],
            colorize=False,
        )
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        ticket_anchor_lines = [
            line for line in body.splitlines() if line.startswith("[acme]") and re.search(r"#\d+", line)
        ]
        assert len(ticket_anchor_lines) <= 1, repr(ticket_anchor_lines)
