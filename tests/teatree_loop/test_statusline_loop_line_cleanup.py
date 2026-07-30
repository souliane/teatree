"""Statusline loop-line cleanup — one dedicated loop line, per-loop ticks, no cruft.

Regression-locks four complaints about the rendered statusline:

1.  Loop info lives on exactly ONE dedicated line — there is no second
    ``loops: tick(Nm)`` fragment elsewhere in the output.
2.  The useless ``N loops live`` headline count is gone. Each live loop
    shows its short name + next tick as a RELATIVE duration in minutes
    (``my-prs 11m · tickets 11m``), never a clock time, never a bare count.
3.  The ``recent_marker: ?`` mystery row (the ``self_update`` scanner's
    internal cadence-gate reason) never reaches the statusline.
4.  MR chips render a terse topic, not the full truncated commit subject —
    a long ``techdebt: refactor PLW0717 try-clause-too-long`` collapses to a
    2-3 word topic.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from teatree.loop.dispatch import DispatchAction, dispatch
from teatree.loop.rendering import zones_for
from teatree.loop.rendering_items import _short_desc
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.statusline import live_loops_anchor, render


def _pr_action(*, url: str, iid: int, title: str, overlay: str = "acme") -> DispatchAction:
    return DispatchAction(
        kind="statusline",
        zone="in_flight",
        detail=f"PR #{iid} {title}",
        payload={"url": url, "iid": iid, "title": title, "overlay": overlay},
    )


class TestPerLoopRelativeTicks:
    """Complaint 2: per-loop name + relative-minutes tick, not ``N loops live``."""

    def test_each_loop_name_and_relative_minutes(self) -> None:
        # Each lease carries its own acquire timestamp; 60s elapsed of the
        # 720s cadence → next tick in 11m (rounded).
        acquired_at = datetime.now(UTC) - timedelta(seconds=60)
        leases = [("loop-my-prs", acquired_at), ("loop-tickets", acquired_at), ("loop-tick", acquired_at)]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
        ):
            lines = live_loops_anchor()
        assert len(lines) == 1, repr(lines)
        line = lines[0]
        # The useless headline count is gone.
        assert "loops live" not in line, line
        # Each live loop's short name appears (the ``loop-`` prefix stripped).
        assert "my-prs" in line, line
        assert "tickets" in line, line
        # Relative minutes, not a clock time. 60s elapsed of 720s → 11m left.
        assert "11m" in line, line
        # No clock-time form (HH:MM) leaked into the line.
        assert ":" not in line.replace("·", ""), line

    def test_per_loop_cadence_yields_different_countdowns(self) -> None:
        # A fast reactive loop and a slow self-improve loop show different
        # countdowns from the same acquire instant (#1400).
        acquired_at = datetime.now(UTC) - timedelta(seconds=60)
        leases = [("loop-slack-answer", acquired_at), ("loop-self-improve", acquired_at)]

        def _cadence(name: str) -> int:
            return 20 if name == "loop-slack-answer" else 1800

        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", side_effect=_cadence),
        ):
            lines = live_loops_anchor()
        line = lines[0]
        # slack-answer: 20s cadence, 60s elapsed → already due.
        assert "slack-answer due" in line, line
        # self-improve: 1800s cadence, 60s elapsed → 29m left.
        assert "self-improve 29m" in line, line

    def test_names_only_when_no_tick_history(self) -> None:
        leases = [("loop-my-prs", None), ("loop-tickets", None)]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
        ):
            lines = live_loops_anchor()
        assert lines == ["my-prs · tickets"], repr(lines)

    def test_empty_when_no_loops_live(self) -> None:
        with patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[]):
            assert live_loops_anchor() == []

    def test_fails_open_on_db_error(self) -> None:
        with patch("teatree.loop.statusline_loops._live_loop_leases", side_effect=RuntimeError("db down")):
            assert live_loops_anchor() == []


class TestSingleLoopLine:
    """Complaint 1: loop info on exactly one line — no duplicate fragment."""

    def test_only_one_loop_line_in_full_render(self, tmp_path: Path) -> None:
        acquired_at = datetime.now(UTC) - timedelta(seconds=60)
        leases = [("loop-my-prs", acquired_at), ("loop-tickets", acquired_at), ("loop-tick", acquired_at)]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
        ):
            zones = zones_for([], colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        loop_lines = [ln for ln in body.splitlines() if "tick 11m" in ln]
        assert len(loop_lines) == 1, body
        # All loop chunks ride that single line.
        assert "my-prs" in loop_lines[0], body
        assert "tickets" in loop_lines[0], body
        # The legacy top-zone ``loops: tick(Nm)`` fragment must not appear.
        assert "loops: tick(" not in body, body
        assert "loops live" not in body, body


class TestRecentMarkerNotRendered:
    """Complaint 3: the ``self_update`` cadence reason never reaches the statusline."""

    def test_recent_marker_dropped(self, tmp_path: Path) -> None:
        signal = ScanSignal(
            kind="self_update.cadence_not_elapsed",
            summary="self-update teatree: cadence_not_elapsed (recent_marker)",
            payload={
                "repo": "teatree",
                "outcome": "cadence_not_elapsed",
                "reason": "recent_marker",
                "old_sha": "",
                "new_sha": "",
            },
        )
        actions = dispatch([signal])
        with patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[]):
            zones = zones_for(actions, colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        assert "recent_marker" not in body, body
        assert "self-update" not in body, body
        assert "self_update" not in body, body

    def test_all_self_update_outcomes_dropped(self) -> None:
        for outcome, reason in (
            ("cadence_not_elapsed", "recent_marker"),
            ("up_to_date", ""),
            ("updated", ""),
            ("skipped", "branch=feature!=main"),
            ("failed", "fetch:boom"),
        ):
            signal = ScanSignal(
                kind=f"self_update.{outcome}",
                summary=f"self-update teatree: {outcome}",
                payload={"repo": "teatree", "outcome": outcome, "reason": reason},
            )
            assert dispatch([signal]) == [], (outcome, dispatch([signal]))


class TestTerseMrTopic:
    """Complaint 4: MR chips show a terse 2-3 word topic, not the full subject."""

    def test_long_commit_subject_collapses_to_topic(self) -> None:
        title = "techdebt: refactor PLW0717 try-clause-too-long across modules"
        topic = _short_desc(title)
        # The terse topic must be short — a few words, not a 40-char truncation
        # of the full commit subject.
        assert len(topic) <= 24, repr(topic)
        assert "across modules" not in topic, repr(topic)
        # The leading conventional-commit type prefix is dropped.
        assert not topic.startswith("techdebt:"), repr(topic)

    def test_terse_topic_in_rendered_chip(self, tmp_path: Path) -> None:
        actions = [
            _pr_action(
                url="https://gitlab.com/x/-/merge_requests/7494",
                iid=7494,
                title="techdebt: refactor PLW0717 try-clause-too-long across modules",
            ),
        ]
        with patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[]):
            zones = zones_for(actions, colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        assert "!7494" in body, body
        # The full truncated subject is gone; no tail-ellipsis of the subject.
        assert "try-clause-t…" not in body, body
        assert "across modules" not in body, body

    def test_short_title_passes_through(self) -> None:
        # A title already terse keeps its words (conventional-commit prefix
        # still stripped for consistency).
        assert _short_desc("loop line cleanup") == "loop line cleanup"
        assert _short_desc("fix: loop line cleanup") == "loop line cleanup"

    def test_empty_title_stays_empty(self) -> None:
        assert _short_desc("") == ""
