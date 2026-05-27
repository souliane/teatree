"""Per-loop next-tick countdowns in the anchors zone (#1400).

The pre-#1400 ``live_loops_anchor()`` collapsed every live loop into one
consolidated ``loop · next tick in <duration> · N loops live`` line, which
hid the per-loop schedule. With multiple configured loops (``loop-tick``,
``loop-owner``, ``loop-slack-answer``, ``loop-self-improve``, …) the
operator could not tell when each one would next fire.

This module locks the new shape: one anchor line per live loop, each with
its own ``next in <duration>`` countdown computed from
``acquired_at + cadence_for_loop(name)``.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from teatree.loop.rendering import zones_for
from teatree.loop.statusline import live_loops_anchor, render


class TestPerLoopAnchorLine:
    """One anchor line per live loop, each with its own next-tick countdown."""

    def test_one_line_per_live_loop(self) -> None:
        leases = [
            ("loop-tick", "sessA"),
            ("loop-slack-answer", "sessA"),
            ("loop-self-improve", "sessA"),
            ("loop-owner", "sessA"),
        ]
        now = datetime.now(UTC)
        acquired_ats = {
            "loop-tick": now - timedelta(seconds=60),
            "loop-slack-answer": now - timedelta(seconds=5),
            "loop-self-improve": now - timedelta(seconds=600),
            "loop-owner": now - timedelta(seconds=120),
        }
        cadences = {
            "loop-tick": 720,
            "loop-slack-answer": 20,
            "loop-self-improve": 1800,
            "loop-owner": 1800,
        }
        with (
            patch("teatree.loop.statusline._live_loop_names", return_value=leases),
            patch("teatree.loop.statusline._loop_acquired_ats", return_value=acquired_ats),
            patch(
                "teatree.loop.statusline._cadence_for_loop",
                side_effect=lambda name: cadences.get(name, 720),
            ),
        ):
            lines = live_loops_anchor()

        assert len(lines) == 4, repr(lines)
        joined = "\n".join(lines)
        assert "loop-tick" in joined
        assert "loop-slack-answer" in joined
        assert "loop-self-improve" in joined
        assert "loop-owner" in joined
        # Each line carries a per-loop next-tick fragment.
        for line in lines:
            assert "next " in line, line

    def test_per_loop_countdown_uses_per_loop_cadence(self) -> None:
        now = datetime.now(UTC)
        leases = [("loop-tick", "sessA"), ("loop-slack-answer", "sessA")]
        acquired_ats = {
            "loop-tick": now - timedelta(seconds=120),
            "loop-slack-answer": now - timedelta(seconds=5),
        }
        cadences = {"loop-tick": 720, "loop-slack-answer": 20}
        with (
            patch("teatree.loop.statusline._live_loop_names", return_value=leases),
            patch("teatree.loop.statusline._loop_acquired_ats", return_value=acquired_ats),
            patch(
                "teatree.loop.statusline._cadence_for_loop",
                side_effect=lambda name: cadences[name],
            ),
        ):
            lines = live_loops_anchor()

        tick_line = next(line for line in lines if "loop-tick" in line)
        slack_line = next(line for line in lines if "loop-slack-answer" in line)

        # loop-tick: 720 - 120 = 600s → 9m59s or 10m (depending on clock jitter).
        assert ("10m" in tick_line) or ("9m" in tick_line), tick_line
        # loop-slack-answer: 20 - 5 = 15s.
        assert ("15s" in slack_line) or ("14s" in slack_line), slack_line

    def test_overdue_loop_reports_due(self) -> None:
        now = datetime.now(UTC)
        leases = [("loop-tick", "sessA")]
        with (
            patch("teatree.loop.statusline._live_loop_names", return_value=leases),
            patch(
                "teatree.loop.statusline._loop_acquired_ats",
                return_value={"loop-tick": now - timedelta(hours=1)},
            ),
            patch("teatree.loop.statusline._cadence_for_loop", return_value=720),
        ):
            lines = live_loops_anchor()

        assert len(lines) == 1
        assert "loop-tick" in lines[0]
        assert "due" in lines[0], lines[0]

    def test_no_acquired_at_yet_reports_never(self) -> None:
        leases = [("loop-tick", "sessA")]
        with (
            patch("teatree.loop.statusline._live_loop_names", return_value=leases),
            patch("teatree.loop.statusline._loop_acquired_ats", return_value={}),
            patch("teatree.loop.statusline._cadence_for_loop", return_value=720),
        ):
            lines = live_loops_anchor()

        assert len(lines) == 1
        assert "loop-tick" in lines[0]
        assert "never" in lines[0], lines[0]

    def test_lines_sorted_stable_by_loop_name(self) -> None:
        leases = [
            ("loop-tick", "sessA"),
            ("loop-owner", "sessA"),
            ("loop-self-improve", "sessA"),
            ("loop-slack-answer", "sessA"),
        ]
        with (
            patch("teatree.loop.statusline._live_loop_names", return_value=leases),
            patch("teatree.loop.statusline._loop_acquired_ats", return_value={}),
            patch("teatree.loop.statusline._cadence_for_loop", return_value=720),
        ):
            lines = live_loops_anchor()

        names = [line.split(" ", 1)[0] for line in lines]
        assert names == sorted(names), names

    def test_no_consolidated_loops_live_summary(self) -> None:
        leases = [("loop-tick", "sessA"), ("loop-owner", "sessA")]
        with (
            patch("teatree.loop.statusline._live_loop_names", return_value=leases),
            patch("teatree.loop.statusline._loop_acquired_ats", return_value={}),
            patch("teatree.loop.statusline._cadence_for_loop", return_value=720),
        ):
            lines = live_loops_anchor()

        joined = "\n".join(lines)
        assert "loops live" not in joined, joined

    def test_empty_when_no_live_loops(self) -> None:
        with patch("teatree.loop.statusline._live_loop_names", return_value=[]):
            assert live_loops_anchor() == []

    def test_fails_open_on_db_error(self) -> None:
        with patch(
            "teatree.loop.statusline._live_loop_names",
            side_effect=RuntimeError("db down"),
        ):
            assert live_loops_anchor() == []

    def test_fails_open_on_acquired_ats_error(self) -> None:
        leases = [("loop-tick", "sessA")]
        with (
            patch("teatree.loop.statusline._live_loop_names", return_value=leases),
            patch(
                "teatree.loop.statusline._loop_acquired_ats",
                side_effect=RuntimeError("db down"),
            ),
            patch("teatree.loop.statusline._cadence_for_loop", return_value=720),
        ):
            lines = live_loops_anchor()
        # Degrades to "never" rather than blanking the line entirely.
        assert len(lines) == 1
        assert "loop-tick" in lines[0]


class TestCadenceForLoop:
    """``_cadence_for_loop(name)`` resolves the per-loop cadence."""

    def test_loop_tick_uses_loop_cadence(self) -> None:
        from teatree.loop.statusline import _cadence_for_loop  # noqa: PLC0415

        with patch("teatree.loop.statusline._cadence_seconds", return_value=720):
            assert _cadence_for_loop("loop-tick") == 720

    def test_loop_slack_answer_uses_slack_answer_cadence(self, monkeypatch) -> None:
        from teatree.loop.statusline import _cadence_for_loop  # noqa: PLC0415

        monkeypatch.setenv("T3_SLACK_ANSWER_CADENCE", "20")
        assert _cadence_for_loop("loop-slack-answer") == 20

    def test_loop_self_improve_uses_self_improve_cadence(self, monkeypatch) -> None:
        from teatree.loop.statusline import _cadence_for_loop  # noqa: PLC0415

        monkeypatch.setenv("T3_SELF_IMPROVE_CHEAP_CADENCE", "1800")
        assert _cadence_for_loop("loop-self-improve") == 1800

    def test_loop_owner_uses_loop_owner_ttl(self, monkeypatch) -> None:
        from teatree.loop.statusline import _cadence_for_loop  # noqa: PLC0415

        monkeypatch.setenv("T3_LOOP_OWNER_TTL", "1800")
        assert _cadence_for_loop("loop-owner") == 1800

    def test_unknown_loop_falls_back_to_default_cadence(self) -> None:
        from teatree.loop.statusline import _cadence_for_loop  # noqa: PLC0415

        with patch("teatree.loop.statusline._cadence_seconds", return_value=720):
            assert _cadence_for_loop("loop-future-feature") == 720


class TestZonesForIntegration:
    """``zones_for`` + ``render`` produce one anchor line per live loop."""

    def test_renders_all_configured_loops(self, tmp_path: Path) -> None:
        leases = [
            ("loop-tick", "sessA"),
            ("loop-slack-answer", "sessA"),
            ("loop-self-improve", "sessA"),
        ]
        now = datetime.now(UTC)
        acquired_ats = {
            "loop-tick": now - timedelta(seconds=60),
            "loop-slack-answer": now - timedelta(seconds=5),
            "loop-self-improve": now - timedelta(seconds=600),
        }
        cadences = {"loop-tick": 720, "loop-slack-answer": 20, "loop-self-improve": 1800}
        with (
            patch("teatree.loop.statusline._live_loop_names", return_value=leases),
            patch("teatree.loop.statusline._loop_acquired_ats", return_value=acquired_ats),
            patch(
                "teatree.loop.statusline._cadence_for_loop",
                side_effect=lambda name: cadences[name],
            ),
        ):
            zones = zones_for([], colorize=False)

        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        assert "loop-tick" in body
        assert "loop-slack-answer" in body
        assert "loop-self-improve" in body
        # The old consolidated summary is gone.
        assert "loops live" not in body
