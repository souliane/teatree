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
