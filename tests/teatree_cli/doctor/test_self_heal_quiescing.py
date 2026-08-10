"""``check_stranded_quiescing_gate`` — a quiesced worker outside a live deploy (#3983).

``worker_quiescing`` is set by the deploy's drain and cleared by the fresh worker's
init. A deploy that dies in between leaves it ON, the claim path admits ZERO work, and
every other health surface stays green — the outage is visible only by reading the flag.
This detector dates the gate against the widest window a live deploy could explain and
hard-FAILs past it, so the doctor exit code the watchdog keys on turns red.
"""

import datetime as dt
import io
import pathlib
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

    def test_a_convergence_still_inside_its_init_wait_is_not_reported_stranded(self) -> None:
        # The staged convergence (#4214) polls init for up to TEATREE_INIT_WAIT_TIMEOUT
        # AFTER the drain that sets the gate and BEFORE the stage-4 drain that would
        # re-date it, so both graces are serial inside one gate-ON window. A budget
        # covering only the drain calls a live deploy a dead one — and the finding is
        # not deploy-sensitive in watchdog.sh, so it pages on sight, mid-update.
        set_worker_quiescing(value=True)
        with mock.patch.dict(
            "os.environ",
            {"TEATREE_DRAIN_TIMEOUT": "1800", "TEATREE_INIT_WAIT_TIMEOUT": "1800"},
        ):
            _age_the_gate(1700 + 1500)  # a long-but-legal drain, then a legal init wait

            ok, out = _echoes(self_heal_quiescing.check_stranded_quiescing_gate)

        assert ok is True, "a convergence inside its own timeouts is an update, not an outage"
        assert out == ""

    def test_the_budget_covers_every_bounded_stage_between_the_drain_and_the_clear(self) -> None:
        stages = {"TEATREE_DRAIN_TIMEOUT": 600, "TEATREE_INIT_WAIT_TIMEOUT": 900, "TEATREE_ADMIN_SWAP_BUDGET": 120}
        with mock.patch.dict("os.environ", {name: str(value) for name, value in stages.items()}):
            budget = self_heal_quiescing.quiescing_deploy_budget_seconds()

        unset = dict(self_heal_quiescing._DEPLOY_STAGE_BUDGETS)["TEATREE_RESUME_TIMEOUT"]

        assert budget >= sum(stages.values()), "a stage the deploy is allowed to spend cannot read as a strand"
        assert budget == sum(stages.values()) + unset + self_heal_quiescing._UNTIMED_STAGE_SLACK_SECONDS

    def test_the_budget_stays_finite_so_a_genuine_strand_still_reddens(self) -> None:
        # The counterweight to widening it: every stage is bounded, so the sum is too.
        assert self_heal_quiescing.quiescing_deploy_budget_seconds() < 4 * 3600

    def test_an_unreadable_stage_timeout_falls_back_to_that_stages_deploy_default(self) -> None:
        with mock.patch.dict("os.environ", {"TEATREE_DRAIN_TIMEOUT": "not-a-number"}):
            polluted = self_heal_quiescing.quiescing_deploy_budget_seconds()
        with mock.patch.dict("os.environ", {}, clear=True):
            clean = self_heal_quiescing.quiescing_deploy_budget_seconds()

        assert polluted == clean

    def test_every_bounded_stage_deploy_sh_runs_in_the_window_is_budgeted(self) -> None:
        # The budget tracks deploy.sh's contract, so drift in either direction — a stage
        # renamed there, or one budgeted here that the deploy never waits on — is a bug.
        deploy_sh = (pathlib.Path(__file__).parents[3] / "deploy" / "deploy.sh").read_text(encoding="utf-8")

        for name, default in self_heal_quiescing._DEPLOY_STAGE_BUDGETS:
            assert f"{name}:-{default}" in deploy_sh, f"{name} default drifted from deploy.sh"
