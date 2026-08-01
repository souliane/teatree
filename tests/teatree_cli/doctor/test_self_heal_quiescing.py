"""``check_stranded_quiescing_gate`` — a quiesced worker outside a live deploy (#3983).

``worker_quiescing`` is set by the deploy's drain and cleared by the fresh worker's
init. A deploy that dies in between leaves it ON, the claim path admits ZERO work, and
every other health surface stays green — the outage is visible only by reading the flag.
This detector dates the gate against the widest window a live deploy could explain and
hard-FAILs past it, so the doctor exit code the watchdog keys on turns red.
"""

import datetime as dt
import io
from collections.abc import Callable
from contextlib import redirect_stdout
from unittest import mock

import django.test
from django.utils import timezone

from teatree.cli.doctor import self_heal, self_heal_quiescing
from teatree.core.models import ConfigSetting
from teatree.loop.drain import QUIESCING_SETTING, set_worker_quiescing


def _echoes(check: Callable[[], bool]) -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = check()
    return ok, buf.getvalue()


def _age_the_gate(seconds: float) -> None:
    """Backdate the gate row's ``updated_at``, bypassing its ``auto_now`` write."""
    ConfigSetting.objects.filter(key=QUIESCING_SETTING).update(
        updated_at=timezone.now() - dt.timedelta(seconds=seconds)
    )


class StrandedQuiescingCheckTest(django.test.TestCase):
    def test_a_clear_gate_passes_silently(self) -> None:
        set_worker_quiescing(value=False)

        ok, out = _echoes(self_heal_quiescing.check_stranded_quiescing_gate)

        assert ok is True
        assert out == ""

    def test_a_freshly_set_gate_is_a_live_deploy_and_passes(self) -> None:
        set_worker_quiescing(value=True)

        ok, out = _echoes(self_heal_quiescing.check_stranded_quiescing_gate)

        assert ok is True, "a drain that just started is a deploy in flight, not a stranded gate"
        assert out == ""

    def test_a_gate_older_than_any_deploy_could_explain_hard_fails(self) -> None:
        set_worker_quiescing(value=True)
        _age_the_gate(self_heal_quiescing.quiescing_deploy_budget_seconds() + 60)

        ok, out = _echoes(self_heal_quiescing.check_stranded_quiescing_gate)

        assert ok is False, "an aged quiescing gate is a zero-admission outage and must redden the doctor"
        assert "FAIL" in out
        assert "worker_quiescing" in out
        assert "config_setting set worker_quiescing false" in out, "the finding must name its own recovery"

    def test_the_finding_is_not_swallowed_by_the_watchdogs_deploy_sensitive_gating(self) -> None:
        # deploy/watchdog.sh drops a finding matching one of these while a convergence
        # is in flight. This one means the convergence ALREADY died, so it must page.
        set_worker_quiescing(value=True)
        _age_the_gate(self_heal_quiescing.quiescing_deploy_budget_seconds() + 60)

        _ok, out = _echoes(self_heal_quiescing.check_stranded_quiescing_gate)

        for gated in ("no loop worker holds the flock", "slack-listener receiver is DOWN", "commit(s) behind origin/"):
            assert gated not in out

    def test_a_gate_no_db_row_can_date_hard_fails(self) -> None:
        # Resolved ON from env/file: nothing can date it, so nothing can call it a live
        # deploy — and a zero-admission factory must never be silent.
        with mock.patch("teatree.config.resolution.worker_is_quiescing", return_value=True):
            ok, out = _echoes(self_heal_quiescing.check_stranded_quiescing_gate)

        assert ok is False
        assert "FAIL" in out

    def test_a_read_error_degrades_to_a_pass(self) -> None:
        with mock.patch("teatree.config.resolution.worker_is_quiescing", side_effect=RuntimeError("boom")):
            ok, out = _echoes(self_heal_quiescing.check_stranded_quiescing_gate)

        assert ok is True, "a detector that aborted the run would recreate the outage it exists to catch"
        assert "WARN" in out

    def test_it_is_wired_into_the_self_heal_sequence(self) -> None:
        set_worker_quiescing(value=True)
        _age_the_gate(self_heal_quiescing.quiescing_deploy_budget_seconds() + 60)

        ok, out = _echoes(self_heal.run_self_heal_checks)

        assert ok is False
        assert "worker_quiescing" in out

    def test_the_budget_covers_the_drain_grace_plus_the_swap(self) -> None:
        with mock.patch.dict("os.environ", {"TEATREE_DRAIN_TIMEOUT": "600"}):
            assert (
                self_heal_quiescing.quiescing_deploy_budget_seconds()
                == 600 + self_heal_quiescing._DEPLOY_SWAP_BUDGET_SECONDS
            )

    def test_an_unreadable_drain_timeout_falls_back_to_the_deploy_default(self) -> None:
        with mock.patch.dict("os.environ", {"TEATREE_DRAIN_TIMEOUT": "not-a-number"}):
            expected = (
                self_heal_quiescing._DEFAULT_DRAIN_TIMEOUT_SECONDS + self_heal_quiescing._DEPLOY_SWAP_BUDGET_SECONDS
            )
            assert self_heal_quiescing.quiescing_deploy_budget_seconds() == expected
