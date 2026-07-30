"""Statusline renderer defects fixed in #1324.

Three regressions visible on a real session statusline:

* stale ticket rendered twice (anchor + action) — drop stale refs whose
    ticket number already appears on the active line for the same overlay.
* ``ready:`` row has no overflow cap — apply ``_MAX_PER_STATE`` + ``(+N)``.
* ``assigned_issue.ready`` signal payload missing ``overlay`` — tracker
    rows surfaced under the wrong overlay zone.
"""

from teatree.loop.dispatch import DispatchAction
from teatree.loop.rendering import zones_for
from teatree.loop.rendering_classification import _classify_actions
from teatree.loop.rendering_zones import _MAX_PER_STATE


def _blob(actions: list[DispatchAction]) -> str:
    zones = zones_for(actions, colorize=False)
    return "\n".join(
        item if isinstance(item, str) else item.text
        for zone in (zones.anchors, zones.action_needed, zones.in_flight)
        for item in zone
    )


def _active_ticket(num: str, state: str, *, url: str = "", overlay: str = "ov") -> DispatchAction:
    return DispatchAction(
        kind="statusline",
        zone="anchors",
        detail=f"#{num}",
        payload={"ticket_number": num, "state": state, "issue_url": url, "overlay": overlay},
    )


def _stale(num: str, *, url: str = "", overlay: str = "ov") -> DispatchAction:
    return DispatchAction(
        kind="statusline",
        zone="action_needed",
        detail=f"#{num} stale",
        payload={
            "stale": True,
            "ticket_number": num,
            "issue_url": url,
            "overlay": overlay,
        },
    )


def _ready(num: str, *, url: str, overlay: str = "ov") -> DispatchAction:
    return DispatchAction(
        kind="statusline",
        zone="action_needed",
        detail=f"Ready to start: #{num}",
        payload={"url": url, "ticket_number": num, "overlay": overlay},
    )


class TestStaleDropsWhenAlsoActive:
    def test_stale_for_active_ticket_does_not_render(self) -> None:
        url = "https://example.com/issues/42"
        actions = [_active_ticket("42", "started", url=url), _stale("42", url=url)]
        blob = _blob(actions)
        assert "#42" in blob
        assert "stale" not in blob

    def test_stale_for_unrelated_ticket_still_renders(self) -> None:
        url = "https://example.com/issues/99"
        actions = [
            _active_ticket("42", "started", url="https://example.com/issues/42"),
            _stale("99", url=url),
        ]
        blob = _blob(actions)
        assert "1 stale" in blob

    def test_per_overlay_isolation_preserves_stale_in_other_overlay(self) -> None:
        actions = [
            _active_ticket("42", "started", url="https://example.com/issues/42", overlay="a"),
            _stale("42", url="https://example.com/issues/42", overlay="b"),
        ]
        c = _classify_actions(actions)
        assert c.stale_refs.get("b")
        assert c.stale_refs["b"][0].label == "#42"
        assert c.stale_refs.get("a") in (None, [])


class TestReadyOverflowCap:
    def test_ready_caps_at_max_per_state_with_overflow_marker(self) -> None:
        n = _MAX_PER_STATE + 3
        actions = [_ready(str(i), url=f"https://example.com/issues/{i}") for i in range(n)]
        blob = _blob(actions)
        # Five rendered, three folded into (+3 more).
        assert "(+3 more)" in blob
        assert "#0" in blob
        assert f"#{_MAX_PER_STATE - 1}" in blob
        assert f"#{_MAX_PER_STATE}" not in blob  # the first overflowed item

    def test_ready_below_cap_renders_no_overflow_marker(self) -> None:
        actions = [_ready(str(i), url=f"https://example.com/issues/{i}") for i in range(3)]
        blob = _blob(actions)
        assert "(+" not in blob
