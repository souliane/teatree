"""Every per-loop tick states its outcome out loud (#3810).

A tick that ran used to emit NOTHING at all — ``_emit_report`` passed
``human=None`` whenever the error map was empty — so a loop that refused work
(SKIP) and a loop that did work were indistinguishable at the console except by
the SKIP line's presence. That silence is why a starving factory looked healthy
for hours. Both outcomes now emit a one-line verdict.
"""

import datetime as dt
import io
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from django.core.management import call_command

from teatree.core.models import Loop, LoopLease
from teatree.loop.tick import TickReport

_LOOP = "inbox"


def _run() -> str:
    out, err = io.StringIO(), io.StringIO()
    call_command("loops_tick", loop=_LOOP, stdout=out, stderr=err)
    return out.getvalue() + err.getvalue()


@pytest.fixture
def _enabled_loop(db: None) -> None:
    Loop.objects.update_or_create(name=_LOOP, defaults={"enabled": True, "delay_seconds": 60})


@pytest.fixture
def _clean_tick() -> Iterator[None]:
    """A tick that completes with no signals, no actions and no errors."""
    report = TickReport(started_at=dt.datetime.now(tz=dt.UTC))
    with (
        patch("teatree.loop.tick.run_tick", return_value=report),
        patch("teatree.loops.connector_preflight.run_loop_connector_preflight"),
    ):
        yield


@pytest.mark.usefixtures("_enabled_loop", "_clean_tick")
class TestTickVerdict:
    def test_a_clean_tick_says_it_ran(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A tick that finds nothing still reports that it ran — silence is not a verdict."""
        monkeypatch.setenv("T3_LOOP_SESSION_ID", "the-only-session")

        output = _run()

        assert "ran   loop 'inbox'" in output, output

    def test_a_skipped_tick_says_why(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A refused tick names the reason, so it is never mistaken for a quiet one."""
        LoopLease.objects.take_over_ownership(f"loop:{_LOOP}", session_id="a-foreign-session", owner_pid=None)
        monkeypatch.setenv("T3_LOOP_SESSION_ID", "the-only-session")

        output = _run()

        assert "SKIP" in output, output
        assert "a-foreign-session" in output, output

    def test_the_two_verdicts_are_distinguishable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole point: a loop that refused work never reads like one that found none."""
        monkeypatch.setenv("T3_LOOP_SESSION_ID", "the-only-session")
        ran = _run()

        LoopLease.objects.take_over_ownership(f"loop:{_LOOP}", session_id="a-foreign-session", owner_pid=None)
        skipped = _run()

        assert ran != skipped
        assert ran.strip(), "a tick that ran must not be silent"
        assert skipped.strip(), "a tick that refused work must not be silent"
