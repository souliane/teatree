"""``manage.py loop_tick`` — the user-manual full-scan tick (autonomous-lane redesign §7).

Drives the real command via ``call_command`` with ``run_tick`` + backends mocked.
Unlike ``loops_tick`` (the per-loop primitive), this by-hand diagnostic requires
no ``--loop``, claims NO owner lease, and runs the FULL default scanner set (no DB
``Loop``-table scoping) so a person can inspect every scanner regardless of which
loops are enabled.
"""

import datetime as dt
import io
import json
from unittest.mock import patch

import django.test
from django.core.management import call_command

from teatree.core.models import LoopLease
from teatree.loop.tick import TickReport

_MOD = "teatree.core.management.commands.loop_tick"


def _run(**kwargs: object) -> str:
    out = io.StringIO()
    call_command("loop_tick", stdout=out, **kwargs)
    return out.getvalue()


class TestManualFullScanTick(django.test.TestCase):
    def test_runs_the_full_default_scan_not_a_scoped_loop(self) -> None:
        report = TickReport(started_at=dt.datetime.now(dt.UTC))
        with (
            patch(f"{_MOD}.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report) as run_tick,
        ):
            _run()
        # No ``jobs_builder`` override → ``run_tick`` falls back to the full default
        # scan (``build_default_jobs``), NOT the scoped DB ``Loop``-table fan-out.
        assert "jobs_builder" not in run_tick.call_args.kwargs

    def test_claims_no_owner_lease(self) -> None:
        report = TickReport(started_at=dt.datetime.now(dt.UTC))
        with (
            patch.object(LoopLease.objects, "claim_ownership") as claim,
            patch(f"{_MOD}.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            _run()
        # User-manual (§7): it never claims the t3-master / per-loop owner lease, so
        # a person can run it by hand from any session, owner or not.
        claim.assert_not_called()

    def test_installs_then_resets_the_schedules_reader(self) -> None:
        report = TickReport(started_at=dt.datetime.now(dt.UTC))
        with (
            patch(f"{_MOD}.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.statusline.set_mini_loop_schedules_reader") as set_reader,
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            _run()
        # Installed for the render, then reset to None so the process-global seam
        # never leaks past the by-hand tick.
        assert set_reader.call_count == 2
        assert set_reader.call_args_list[-1].args == (None,)

    def test_json_output_carries_the_report_contract(self) -> None:
        report = TickReport(started_at=dt.datetime.now(dt.UTC))
        with (
            patch(f"{_MOD}.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            out = _run(json_output=True)
        payload = json.loads(out)
        assert payload["signal_count"] == 0
        assert "errors" in payload

    def test_overlay_flag_builds_a_single_overlay_request(self) -> None:
        report = TickReport(started_at=dt.datetime.now(dt.UTC))
        with (
            patch(f"{_MOD}.code_host_from_overlay") as host,
            patch(f"{_MOD}.messaging_from_overlay") as messaging,
            patch(f"{_MOD}.iter_overlay_backends") as iter_backends,
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            _run(overlay="teatree")
        # ``--overlay`` builds a single-overlay request (host + messaging), never the
        # all-overlay backends bundle.
        host.assert_called_once()
        messaging.assert_called_once()
        iter_backends.assert_not_called()
