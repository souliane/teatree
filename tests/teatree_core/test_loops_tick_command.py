"""``manage.py loops_tick`` — the master tick (#1796).

Drives the real command via ``call_command`` with the dispatch pipeline and
backends mocked. Asserts the ``t3-master`` ownership gate (non-owner SKIPs) and
that the won path runs ``run_tick`` with the DB-``Loop``-driven jobs builder.
"""

import datetime as dt
import io
from unittest.mock import patch

import django.test
from django.core.management import call_command

from teatree.core.management.commands.loops_tick import _loop_table_jobs_builder
from teatree.core.models import LoopLease
from teatree.loop.tick import TickReport


def _run(**kwargs: object) -> str:
    out = io.StringIO()
    call_command("loops_tick", stdout=out, **kwargs)
    return out.getvalue()


class TestLoopsTickOwnership(django.test.TestCase):
    def test_non_owner_session_skips(self) -> None:
        with (
            patch("teatree.core.connector_preflight.run_connector_preflight"),
            patch.object(LoopLease.objects, "claim_ownership", return_value=(False, "other-session")),
            patch("teatree.loop.tick.run_tick") as run_tick,
        ):
            out = _run()
        assert "SKIP" in out
        run_tick.assert_not_called()

    def test_won_owner_runs_master_tick_with_loop_table_builder(self) -> None:
        report = TickReport(started_at=dt.datetime.now(dt.UTC))
        with (
            patch("teatree.core.connector_preflight.run_connector_preflight"),
            patch.object(LoopLease.objects, "claim_ownership", return_value=(True, "me")),
            patch.object(LoopLease.objects, "acquire", return_value=True),
            patch.object(LoopLease.objects, "release"),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report) as run_tick,
            patch("teatree.loop.tick_piggyback.run_piggyback_cycles"),
        ):
            _run()
        assert run_tick.called
        assert run_tick.call_args.kwargs["jobs_builder"] is _loop_table_jobs_builder
