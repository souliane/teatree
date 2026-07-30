"""A per-loop tick whose loop the loop-table REFUSED says so — never ``ran`` (#3843).

#3810 made a tick that ran state its outcome out loud, but only the *lease*
refusals reached ``_emit_skip``. A loop the loop-table itself declined to
dispatch — force-OFF, held, disabled, not due, colleague-facing under an away
mode — still produced an empty job list, and the command rendered that as
``ran loop 'review' — 0 signal(s), 0 action(s)``.

That is exactly how the ``review`` loop sat force-OFF for hours while every tick
reported success: the console could not tell "the control plane refused this
loop" from "the loop swept its PRs and found nothing to do".

The tick here is the REAL pipeline (no ``run_tick`` stub) over a stub registry —
the verdict is only trustworthy if it is the one the live jobs builder produced.
"""

import io
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import django.test
from django.core.management import call_command

from teatree.core.models import Loop, LoopState
from teatree.loops.base import MiniLoop

_LOOP = "review"


@contextmanager
def _tick_env() -> Iterator[Path]:
    """The live tick pipeline over a stub ``review`` mini-loop that scans nothing."""
    stub = MiniLoop(name=_LOOP, default_cadence_seconds=60, build_jobs=lambda **_: [])
    with (
        tempfile.TemporaryDirectory() as tmp,
        patch("teatree.loops.loop_table.iter_loops", return_value=(stub,)),
        patch("teatree.core.management.commands.loops_tick.iter_overlay_backends", return_value=[]),
        patch("teatree.loops.connector_preflight.run_loop_connector_preflight"),
        patch.dict(os.environ, {"T3_LOOP_SESSION_ID": "the-only-session"}),
    ):
        yield Path(tmp) / "statusline.txt"


def _run(*, json_output: bool = False) -> str:
    out, err = io.StringIO(), io.StringIO()
    with _tick_env() as statusline:
        call_command(
            "loops_tick",
            loop=_LOOP,
            statusline_file=statusline,
            json_output=json_output,
            stdout=out,
            stderr=err,
        )
    return out.getvalue() + err.getvalue()


class TestBlockedLoopVerdict(django.test.TestCase):
    def setUp(self) -> None:
        Loop.objects.update_or_create(
            name=_LOOP,
            defaults={"enabled": True, "delay_seconds": 60, "script": f"src/teatree/loops/{_LOOP}/loop.py"},
        )

    def test_a_force_off_loop_is_reported_as_skipped_not_run(self) -> None:
        LoopState.objects.override(_LOOP, on=False, reason="held until the repo variable is corrected")

        output = _run()

        assert "SKIP" in output, output
        assert "forced OFF" in output, output
        assert "ran   loop" not in output, output

    def test_the_skip_names_the_command_that_lifts_it(self) -> None:
        """A reason the operator cannot act on is only half an answer."""
        LoopState.objects.override(_LOOP, on=False, reason="emergency")

        assert "t3 loop override review clear" in _run()

    def test_a_held_loop_is_reported_as_skipped_not_run(self) -> None:
        LoopState.objects.pause(_LOOP)

        output = _run()

        assert "SKIP" in output, output
        assert "held" in output.lower(), output

    def test_a_disabled_loop_is_reported_as_skipped_not_run(self) -> None:
        Loop.objects.filter(name=_LOOP).update(enabled=False)

        output = _run()

        assert "SKIP" in output, output
        assert "disabled" in output.lower(), output

    def test_a_dispatched_loop_still_reports_that_it_ran(self) -> None:
        output = _run()

        assert "ran   loop 'review'" in output, output
        assert "SKIP" not in output, output

    def test_the_two_verdicts_are_distinguishable(self) -> None:
        """The regression this pins: both used to print the identical ``ran`` line."""
        ran = _run()
        LoopState.objects.override(_LOOP, on=False, reason="emergency")
        blocked = _run()

        assert ran != blocked
        assert ran.strip(), "a tick that ran must not be silent"
        assert blocked.strip(), "a tick the control plane refused must not be silent"

    def test_the_structured_report_marks_the_refusal_as_skipped(self) -> None:
        """A JSON consumer reads the same distinction the console does."""
        LoopState.objects.override(_LOOP, on=False, reason="emergency")

        payload = json.loads(_run(json_output=True))

        assert payload["skipped"] is True
        assert "forced OFF" in payload["skipped_reason"]

    def test_the_structured_report_of_a_real_run_is_not_skipped(self) -> None:
        payload = json.loads(_run(json_output=True))

        assert payload["skipped"] is False
        assert payload["skipped_reason"] == ""
