"""Tests for teatree.loop.rendering — line builder under NO_COLOR (#721).

The statusline module documents NO_COLOR (https://no-color.org/) support,
but ``rendering._link`` baked OSC 8 hyperlink escapes into the line text
*before* ``render()`` could honour ``colorize=False`` — so a NO_COLOR
consumer (or anything parsing the file as plain text) got escape-byte
garbage and no ``text <url>`` fallback. These drive the full
``zones_for`` → ``_link`` → ``render`` pipeline under NO_COLOR.
"""

from pathlib import Path

from teatree.loop.dispatch import DispatchAction
from teatree.loop.rendering import zones_for
from teatree.loop.statusline import render

_ACTIONS = [
    DispatchAction(
        kind="statusline",
        zone="action_needed",
        detail="Ticket 55 — issue_closed",
        payload={
            "reason": "issue_closed",
            "overlay": "teatree",
            "url": "https://example.com/issues/55",
        },
    ),
]


class TestNoColorPipeline:
    def test_zones_for_colorize_false_emits_no_escapes(self) -> None:
        zones = zones_for(_ACTIONS, colorize=False)
        blob = "".join(
            item if isinstance(item, str) else item.text
            for zone in (zones.anchors, zones.action_needed, zones.in_flight)
            for item in zone
        )
        assert "\033" not in blob, repr(blob)
        # The URL must still be present, as a plain `text <url>` fallback.
        assert "https://example.com/issues/55" in blob

    def test_zones_for_colorize_true_keeps_osc8(self) -> None:
        zones = zones_for(_ACTIONS, colorize=True)
        blob = "".join(
            item if isinstance(item, str) else item.text
            for zone in (zones.anchors, zones.action_needed, zones.in_flight)
            for item in zone
        )
        assert "\033]8;;" in blob

    def test_full_render_under_no_color_has_zero_escape_bytes(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        target = tmp_path / "statusline.txt"
        zones = zones_for(_ACTIONS, colorize=False)
        render(zones, target=target, colorize=False)
        content = target.read_text(encoding="utf-8")
        assert "\033" not in content, repr(content)
        assert "https://example.com/issues/55" in content

    def test_zones_for_defaults_to_env_when_colorize_none(self, monkeypatch) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        zones = zones_for(_ACTIONS)  # colorize unset -> resolve from env
        blob = "".join(
            item if isinstance(item, str) else item.text
            for zone in (zones.anchors, zones.action_needed, zones.in_flight)
            for item in zone
        )
        assert "\033" not in blob, repr(blob)

    def test_zones_for_default_colorizes_without_no_color(self, monkeypatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        zones = zones_for(_ACTIONS)
        blob = "".join(
            item if isinstance(item, str) else item.text
            for zone in (zones.anchors, zones.action_needed, zones.in_flight)
            for item in zone
        )
        assert "\033]8;;" in blob


def _statusline_action(*, detail: str, url: str) -> DispatchAction:
    """Build a reviewer-pr dual-dispatch statusline action.

    Mirrors what ``dispatch._dispatch_one`` produces: ``detail`` is the
    scanner summary and the payload carries ``url``/``overlay`` but no
    ``iid`` (reviewer scanners never ship an iid — see
    ``scanners/reviewer_prs.py``).
    """
    return DispatchAction(
        kind="statusline",
        zone="action_needed",
        detail=detail,
        payload={"url": url, "overlay": "teatree"},
    )


class TestReviewerPrRefRendering:
    """Reviewer-pr signals render as a clickable per-overlay ``!N`` ref.

    They carry only ``url`` (no ``iid``); the renderer derives the iid
    from the URL tail. ``approval_dismissed`` uses a different summary
    ("Approval dismissed:") than new_sha/unreviewed ("Review needed:"),
    so the derivation must cover both prefixes.
    """

    _PR_URL = "https://gitlab.example.com/g/p/-/merge_requests/123"

    def test_review_needed_renders_clickable_pr_ref(self) -> None:
        action = _statusline_action(detail=f"Review needed: {self._PR_URL}", url=self._PR_URL)
        zones = zones_for([action], colorize=False)
        action_blob = "".join(item if isinstance(item, str) else item.text for item in zones.action_needed)
        assert "[teatree] !123" in action_blob, repr(action_blob)

    def test_approval_dismissed_renders_clickable_pr_ref(self) -> None:
        zones = zones_for(
            [_statusline_action(detail=f"Approval dismissed: {self._PR_URL}", url=self._PR_URL)],
            colorize=False,
        )
        action_blob = "".join(item if isinstance(item, str) else item.text for item in zones.action_needed)
        # Before the fix this collapsed into a generic "Approval dismissed:
        # <url>" line with no per-overlay `!N` grouping.
        assert "[teatree] !123" in action_blob, repr(action_blob)


def _disposition_action(*, reason: str, payload_extra: dict[str, object]) -> DispatchAction:
    """Mirror what ``dispatch._dispatch_one`` builds for ``unassigned``/etc.

    The disposition scanner ships ``reason`` plus the ticket coordinates;
    ``payload_extra`` adds the reason-specific fields (``old_owner`` /
    ``new_owners`` for ``unassigned``).
    """
    return DispatchAction(
        kind="statusline",
        zone="action_needed",
        detail=f"Ticket 77 — {reason}",
        payload={
            "reason": reason,
            "overlay": "teatree",
            "ticket_number": "77",
            "issue_url": "https://example.com/issues/77",
            **payload_extra,
        },
    )


class TestReassignedShowsFromTo:
    """Ask 1 — ``reassigned`` must spell out the ownership transition.

    A bare ``reassigned: #77`` told the user nothing. The line now reads
    ``reassigned (from <old> → to <new>): #77`` using the owner identities
    the disposition scanner already had at detection time.
    """

    def test_reassigned_renders_from_and_to_owners(self) -> None:
        action = _disposition_action(
            reason="unassigned",
            payload_extra={"old_owner": "alice", "new_owners": ["bob"]},
        )
        zones = zones_for([action], colorize=False)
        blob = "".join(item if isinstance(item, str) else item.text for item in zones.action_needed)
        assert "reassigned (from alice → to bob):" in blob, repr(blob)
        assert "#77" in blob

    def test_reassigned_joins_multiple_new_owners(self) -> None:
        action = _disposition_action(
            reason="unassigned",
            payload_extra={"old_owner": "alice", "new_owners": ["bob", "carol"]},
        )
        zones = zones_for([action], colorize=False)
        blob = "".join(item if isinstance(item, str) else item.text for item in zones.action_needed)
        assert "reassigned (from alice → to bob, carol):" in blob, repr(blob)

    def test_reassigned_without_owner_data_is_still_labelled(self) -> None:
        """Old signals (no owner fields) must not regress to a bare token."""
        action = _disposition_action(reason="unassigned", payload_extra={})
        zones = zones_for([action], colorize=False)
        blob = "".join(item if isinstance(item, str) else item.text for item in zones.action_needed)
        assert "reassigned:" in blob, repr(blob)
        assert "from " not in blob


def _stale_action(*, number: str, state: str, age: int, overlay: str = "teatree") -> DispatchAction:
    """Mirror dispatch output for a ``ticket.stale`` signal."""
    return DispatchAction(
        kind="statusline",
        zone="action_needed",
        detail=f"#{number} stale ({age}d)",
        payload={
            "stale": True,
            "overlay": overlay,
            "ticket_number": number,
            "ticket_state": state,
            "age_days": age,
            "issue_url": f"https://example.com/issues/{number}",
        },
    )


class TestStaleTicketsConciseAndLinked:
    """Asks 2 & 3 — stale tickets collapse to one concise, linked line.

    Before: one verbose unlinked ``TICKET-N stale in STATE (3d)`` line per
    ticket (the red sprawl). After: a single ``N stale: #a #b #c`` row per
    overlay with every ref a clickable link.
    """

    def test_multiple_stale_tickets_collapse_to_one_line(self) -> None:
        actions = [
            _stale_action(number="58", state="coded", age=4),
            _stale_action(number="724", state="started", age=6),
            _stale_action(number="878", state="tested", age=9),
        ]
        zones = zones_for(actions, colorize=False)
        # One line for the overlay, not three.
        assert len(zones.action_needed) == 1, repr(zones.action_needed)
        line = zones.action_needed[0]
        text = line if isinstance(line, str) else line.text
        assert "[teatree] 3 stale:" in text, repr(text)
        for ref in ("#58", "#724", "#878"):
            assert ref in text, repr(text)
        # Concise: the verbose per-ticket phrasing must be gone.
        assert "stale in" not in text

    def test_stale_refs_are_clickable_links(self) -> None:
        zones = zones_for([_stale_action(number="58", state="coded", age=4)], colorize=False)
        line = zones.action_needed[0]
        text = line if isinstance(line, str) else line.text
        # NO_COLOR fallback form proves the URL is attached to the ref.
        assert "#58 <https://example.com/issues/58>" in text, repr(text)

    def test_stale_lines_split_per_overlay(self) -> None:
        actions = [
            _stale_action(number="58", state="coded", age=4, overlay="teatree"),
            _stale_action(number="9", state="coded", age=5, overlay="acme"),
        ]
        zones = zones_for(actions, colorize=False)
        texts = sorted(item if isinstance(item, str) else item.text for item in zones.action_needed)
        assert any(t.startswith("[acme] 1 stale:") for t in texts), repr(texts)
        assert any(t.startswith("[teatree] 1 stale:") for t in texts), repr(texts)

    def test_stale_osc8_hyperlink_when_colorized(self) -> None:
        zones = zones_for([_stale_action(number="58", state="coded", age=4)], colorize=True)
        line = zones.action_needed[0]
        text = line if isinstance(line, str) else line.text
        assert "\033]8;;https://example.com/issues/58" in text, repr(text)
