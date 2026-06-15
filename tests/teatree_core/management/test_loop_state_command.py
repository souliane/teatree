"""``manage.py loop_state`` — pause/resume/disable/enable a mini-loop (#1913).

Integration-first against the real DB via ``call_command``: each subcommand
performs the atomic ``LoopState`` transition and the transition is idempotent
(re-issuing it is a no-op that leaves the one row in the target status). The
command re-reads and reports the landed status so the operator sees the verified
state, not just an echo of the request.
"""

import json
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import LoopState, LoopStatus


def _run(*args: str) -> str:
    out = StringIO()
    call_command("loop_state", *args, stdout=out)
    return out.getvalue()


class TestLoopStateCommand(TestCase):
    def test_pause_sets_paused_status(self) -> None:
        _run("pause", "review")
        assert LoopState.objects.status_of("review") is LoopStatus.PAUSED

    def test_disable_sets_disabled_status(self) -> None:
        _run("disable", "ship")
        assert LoopState.objects.status_of("ship") is LoopStatus.DISABLED

    def test_resume_returns_to_enabled(self) -> None:
        _run("pause", "review")
        _run("resume", "review")
        assert LoopState.objects.status_of("review") is LoopStatus.ENABLED

    def test_enable_returns_to_enabled_from_disabled(self) -> None:
        _run("disable", "ship")
        _run("enable", "ship")
        assert LoopState.objects.status_of("ship") is LoopStatus.ENABLED

    def test_pause_is_idempotent(self) -> None:
        _run("pause", "review")
        _run("pause", "review")
        assert LoopState.objects.filter(name="review", status=LoopStatus.PAUSED).count() == 1

    def test_output_reports_the_landed_status(self) -> None:
        out = _run("pause", "review")
        assert "review" in out
        assert "paused" in out.lower()

    def test_json_output_carries_name_and_status(self) -> None:
        out = _run("disable", "ship", "--json")
        payload = json.loads(out)
        assert payload["name"] == "ship"
        assert payload["status"] == "disabled"

    def test_status_subcommand_reports_enabled_for_untouched_loop(self) -> None:
        out = _run("status", "never-touched", "--json")
        payload = json.loads(out)
        assert payload["name"] == "never-touched"
        assert payload["status"] == "enabled"
