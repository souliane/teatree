"""Tests for the ``loop_tick`` Django management command."""

import datetime as dt
import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands.loop_tick import _report_to_dict
from teatree.loop.dispatch import DispatchAction
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.tick import TickReport


def _build_report(*, statusline_path: Path | None = None, errors: dict[str, str] | None = None) -> TickReport:
    return TickReport(
        started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        signals=[ScanSignal(kind="my_pr.open", summary="x")],
        actions=[DispatchAction(kind="statusline", zone="in_flight", detail="x")],
        statusline_path=statusline_path,
        errors=errors or {},
    )


class TestReportToDict(TestCase):
    def test_serialises_full_report(self) -> None:
        report = _build_report(statusline_path=Path("/tmp/sl.txt"), errors={"my_prs": "boom"})

        data = _report_to_dict(report)

        assert data["started_at"] == "2026-01-01T00:00:00+00:00"
        assert data["signal_count"] == 1
        assert data["action_count"] == 1
        assert data["statusline_path"].endswith("sl.txt")
        assert data["errors"] == {"my_prs": "boom"}
        assert data["actions"][0]["zone"] == "in_flight"

    def test_empty_statusline_path_serialises_to_empty_string(self) -> None:
        report = _build_report(statusline_path=None)

        data = _report_to_dict(report)

        assert data["statusline_path"] == ""


class TestLoopTickCommand(TestCase):
    def test_text_output(self) -> None:
        report = _build_report(statusline_path=Path("/tmp/sl.txt"))
        stdout = StringIO()
        with (
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            call_command("loop_tick", stdout=stdout)

        output = stdout.getvalue()
        assert "1 signal(s)" in output
        assert "statusline" in output

    def test_text_output_includes_scanner_errors(self) -> None:
        report = _build_report(errors={"my_prs": "RuntimeError: x"})
        stdout = StringIO()
        with (
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            call_command("loop_tick", stdout=stdout)

        output = stdout.getvalue()
        assert "WARN  my_prs" in output

    def test_json_output(self) -> None:
        report = _build_report(statusline_path=Path("/tmp/sl.txt"))
        stdout = StringIO()
        with (
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            call_command("loop_tick", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        assert payload["signal_count"] == 1
        assert payload["action_count"] == 1

    def test_overlay_option_uses_single_overlay_path(self) -> None:
        report = _build_report()
        stdout = StringIO()
        with (
            patch("teatree.core.backend_factory.code_host_from_overlay", return_value=None) as host_mock,
            patch("teatree.core.backend_factory.messaging_from_overlay", return_value=None),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            call_command("loop_tick", "--overlay", "myoverlay", stdout=stdout)

        host_mock.assert_called_once()

    def test_skips_tick_when_lease_held_by_another_owner(self) -> None:
        """A live DB lease held by another owner makes the command skip (#786 WS2).

        No mock of the lease — a genuine concurrent holder is simulated by
        acquiring the real ``loop-tick`` ``LoopLease`` as a rival owner
        first, exercising the exact production CAS-refusal path that
        replaced the flock.
        """
        from teatree.core.models import LoopLease  # noqa: PLC0415

        assert LoopLease.objects.acquire("loop-tick", owner="rival-tick") is True
        stdout = StringIO()
        with patch("teatree.loop.tick.run_tick") as run_tick_mock:
            call_command("loop_tick", stdout=stdout)

        run_tick_mock.assert_not_called()
        output = stdout.getvalue()
        assert "SKIP" in output
        assert "another tick is already running" in output

    def test_skip_json_emits_full_contract_shape(self) -> None:
        """#744 defect 1: a skipped tick's --json must be contract-shaped.

        A coordinator that pumps ``t3 loop tick --json`` and reads
        ``["signal_count"]`` / ``["errors"]`` must not ``KeyError`` on a
        skipped tick (lease held by a sibling). The skip payload carries
        the full contract keys (zeroed) plus an explicit skipped flag.
        """
        import tempfile  # noqa: PLC0415

        from teatree.core.models import LoopLease  # noqa: PLC0415

        assert LoopLease.objects.acquire("loop-tick", owner="rival-tick") is True
        stdout = StringIO()
        with tempfile.TemporaryDirectory() as d:
            # Isolate the tick-meta freshness-touch off the real
            # ~/.local/share path during the test.
            call_command("loop_tick", "--json", "--statusline-file", str(Path(d) / "sl.txt"), stdout=stdout)

        payload = json.loads(stdout.getvalue())
        assert payload["signal_count"] == 0
        assert payload["action_count"] == 0
        assert payload["errors"] == {}
        assert payload["actions"] == []
        assert "started_at" in payload
        assert "statusline_path" in payload
        assert payload["skipped"] is True
        assert "another tick is already running" in payload["skipped_reason"]

    def test_skip_refreshes_tick_meta_so_no_false_stale(self) -> None:
        """#744 defect 2: a skipped tick must keep tick-meta fresh.

        The lease is held by a *sibling* tick keeping the loop alive,
        so a skip must advance ``tick-meta.json``'s ``next_epoch`` —
        otherwise it decays past ``now + 2*cadence`` and the statusline
        renders a false ``tick stale`` under normal multi-session
        contention.
        """
        import datetime as _dt  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        from teatree.core.models import LoopLease  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as d:
            sl = Path(d) / "statusline.txt"
            meta = sl.with_name("tick-meta.json")
            assert LoopLease.objects.acquire("loop-tick", owner="rival-tick") is True
            before = int(_dt.datetime.now(tz=_dt.UTC).timestamp())
            call_command("loop_tick", "--statusline-file", str(sl), stdout=StringIO())

            assert meta.exists(), "skipped tick did not write tick-meta.json — false 'tick stale' will follow"
            payload = json.loads(meta.read_text(encoding="utf-8"))
            assert payload["next_epoch"] >= before, payload
