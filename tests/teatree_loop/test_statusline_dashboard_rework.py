"""Statusline dashboard rework (#130) — leak drop, FSM-state grouping, loop line.

Regression-locks the dashboard contract the user kept hitting:

1.  Scanner bookkeeping signals (``pr_sweep.*``, ``pull_main_clone.*``,
    ``outbound.*``, ``review_nag.*``, ``architectural_review.*``,
    ``dogfood_smoke.*``, ``scanning_news.*``) are internal state, not
    user-facing rows — they must NOT produce a statusline action, and a
    leaked all-``?`` disposition must never render.
2.  The dim anchor line groups tickets BY FSM STATE with a restored
    ``state:`` label (``coded:`` / ``tested:`` / ``scoped:`` …), joined by
    `` · `` and ordered by state priority — one physical line per overlay.
3.  The single loop line carries a leading state word (``running`` / ``idle``)
    and a ``waiting=N`` clause when things wait on the user.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.loop.dispatch import dispatch
from teatree.loop.rendering import zones_for
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.statusline import live_loops_anchor, render

_LEAKING_SIGNALS: tuple[tuple[str, dict[str, str]], ...] = (
    ("pr_sweep.merged", {"repo": "teatree", "reason": "merged"}),
    ("pull_main_clone.pulled", {"repo": "teatree"}),
    ("outbound.drift", {"overlay": "acme", "reason": "drift"}),
    ("review_nag.ping", {"overlay": "acme", "reason": "ping"}),
    ("review_nag.stale_no_dm", {"overlay": "acme", "reason": "stale_no_dm"}),
    ("review_request_merge_react.reacted", {"overlay": "acme", "mr_url": "https://x/pull/1"}),
    ("review_request_merge_react.react_failed", {"overlay": "acme", "error": "not_in_channel"}),
    ("architectural_review.queued", {"overlay": "acme"}),
    ("dogfood_smoke.queued", {"overlay": "acme"}),
    ("scanning_news.queued", {}),
)


class TestScannerBookkeepingDropped:
    """Concern 1: internal scanner signals never produce a statusline row."""

    @pytest.mark.parametrize(("kind", "payload"), _LEAKING_SIGNALS)
    def test_leaking_kind_produces_no_action(self, kind: str, payload: dict[str, str]) -> None:
        signal = ScanSignal(kind=kind, summary=f"{kind}: bookkeeping", payload=payload)
        assert dispatch([signal]) == [], (kind, dispatch([signal]))

    def test_leaking_kinds_absent_from_rendered_statusline(self, tmp_path: Path) -> None:
        signals = [ScanSignal(kind=kind, summary=f"{kind}: x", payload=p) for kind, p in _LEAKING_SIGNALS]
        actions = dispatch(signals)
        with patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[]):
            zones = zones_for(actions, colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        leaking_fragments = (
            "pr_sweep",
            "review_nag",
            "review_request_merge_react",
            "architectural_review",
            "dogfood_smoke",
            "scanning_news",
            "outbound",
            "pull_main_clone",
        )
        for fragment in leaking_fragments:
            assert fragment not in body, (fragment, body)


class TestMergeReactMissingScopeSurfaces:
    """The merged-review-request missing-scope config gap reaches the statusline.

    The rest of the ``review_request_merge_react.*`` family is dropped as
    per-MR bookkeeping; ``missing_scope`` is the one outcome an operator
    must act on (the personal xoxp token lacks ``reactions:write``), so it
    is exempted from the drop and renders in ``action_needed``.
    """

    def test_missing_scope_produces_an_action(self) -> None:
        signal = ScanSignal(
            kind="review_request_merge_react.missing_scope",
            summary="needs reactions:write",
            payload={"overlay": "acme", "needed": "reactions:write"},
        )
        actions = dispatch([signal])
        assert actions, actions
        assert any(a.zone == "action_needed" for a in actions), actions


class TestBareQuestionDispositionDropped:
    """Concern 1 defense-in-depth: an all-``?`` disposition never renders."""

    def test_disposition_with_only_question_refs_dropped(self, tmp_path: Path) -> None:
        # A reason-bearing action whose ref resolves to the bare ``?`` token
        # (no url, no ticket_number, no title) must not surface as a
        # ``<reason>: ?`` row.
        signal = ScanSignal(
            kind="ticket.disposition_candidate",
            summary="mystery disposition",
            payload={"overlay": "acme", "reason": "label_removed"},
        )
        actions = dispatch([signal])
        with patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[]):
            zones = zones_for(actions, colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        assert "label-removed: ?" not in body, body
        assert ": ?" not in body, body

    def test_disposition_with_real_ref_survives(self, tmp_path: Path) -> None:
        signal = ScanSignal(
            kind="ticket.disposition_candidate",
            summary="real disposition",
            payload={
                "overlay": "acme",
                "reason": "label_removed",
                "issue_url": "https://gitlab.com/x/-/issues/4242",
            },
        )
        actions = dispatch([signal])
        with patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[]):
            zones = zones_for(actions, colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        assert "#4242" in body, body


def _active_ticket(*, number: str, state: str, overlay: str = "acme", title: str = "") -> ScanSignal:
    return ScanSignal(
        kind="ticket.active",
        summary=f"ticket {number} {state}",
        payload={
            "ticket_number": number,
            "state": state,
            "overlay": overlay,
            "issue_url": f"https://gitlab.com/x/-/issues/{number}",
            "title": title,
        },
    )


class TestFsmStateGroupingLabels:
    """Concern 2: anchor line groups by FSM state with restored ``state:`` labels."""

    def _render_anchor(self, signals: list[ScanSignal], tmp_path: Path) -> str:
        actions = dispatch(signals)
        with patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[]):
            zones = zones_for(actions, colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        return target.read_text()

    def test_state_labels_present_and_grouped(self, tmp_path: Path) -> None:
        body = self._render_anchor(
            [
                _active_ticket(number="8495", state="coded", title="extra topic"),
                _active_ticket(number="8470", state="tested", title="csv export"),
                _active_ticket(number="8540", state="scoped", title="loan stage filter"),
            ],
            tmp_path,
        )
        anchor = next(ln for ln in body.splitlines() if "#8495" in ln)
        assert "coded:" in anchor, anchor
        assert "tested:" in anchor, anchor
        assert "scoped:" in anchor, anchor
        # State priority order: coded before tested before scoped.
        assert anchor.index("coded:") < anchor.index("tested:") < anchor.index("scoped:"), anchor

    def test_single_anchor_line_per_overlay_across_states(self, tmp_path: Path) -> None:
        body = self._render_anchor(
            [
                _active_ticket(number="1", state="coded"),
                _active_ticket(number="2", state="tested"),
                _active_ticket(number="3", state="scoped"),
                _active_ticket(number="4", state="started"),
            ],
            tmp_path,
        )
        acme_lines = [ln for ln in body.splitlines() if ln.startswith("[acme]")]
        assert len(acme_lines) == 1, body

    def test_states_joined_by_middot(self, tmp_path: Path) -> None:
        body = self._render_anchor(
            [
                _active_ticket(number="1", state="coded"),
                _active_ticket(number="2", state="tested"),
            ],
            tmp_path,
        )
        anchor = next(ln for ln in body.splitlines() if ln.startswith("[acme]"))
        assert " · " in anchor, anchor


class TestLoopLineStateAndWaiting:
    """Concern 3: loop line leads with its loop chunks + a ``waiting:`` clause."""

    def test_line_leads_with_loop_chunk_when_live(self) -> None:
        acquired_at = datetime.now(UTC) - timedelta(seconds=60)
        leases = [("loop-tickets", acquired_at)]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch("teatree.loop.statusline_loops._availability_segment", return_value=""),
            patch("teatree.loop.statusline_loops._waiting_count", return_value=0),
        ):
            lines = live_loops_anchor()
        assert len(lines) == 1, repr(lines)
        assert lines[0].startswith("tickets"), lines[0]
        assert "loop running" not in lines[0], lines[0]

    def test_waiting_clause_when_blocked_on_user(self) -> None:
        acquired_at = datetime.now(UTC) - timedelta(seconds=60)
        leases = [("loop-tickets", acquired_at)]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch("teatree.loop.statusline_loops._waiting_count", return_value=2),
        ):
            lines = live_loops_anchor()
        assert "2 waiting" in lines[0], lines[0]

    def test_no_waiting_clause_when_no_pending(self) -> None:
        acquired_at = datetime.now(UTC) - timedelta(seconds=60)
        leases = [("loop-tickets", acquired_at)]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch("teatree.loop.statusline_loops._waiting_count", return_value=0),
        ):
            lines = live_loops_anchor()
        assert "waiting" not in lines[0], lines[0]

    def test_waiting_count_failure_is_fail_open(self) -> None:
        acquired_at = datetime.now(UTC) - timedelta(seconds=60)
        leases = [("loop-tickets", acquired_at)]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch("teatree.loop.statusline_loops._availability_segment", return_value=""),
            patch("teatree.loop.statusline_loops._waiting_count", side_effect=RuntimeError("db down")),
        ):
            lines = live_loops_anchor()
        assert lines[0].startswith("tickets"), lines[0]
        assert "waiting" not in lines[0], lines[0]
