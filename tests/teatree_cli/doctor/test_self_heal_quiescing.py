"""``check_stranded_quiescing_gate`` — a quiesced worker outside a live deploy (#3983, #4359).

``worker_quiescing`` is set by the deploy's drain and cleared by the fresh worker's
init. A deploy that dies in between leaves it ON, the claim path admits ZERO work, and
every other health surface stays green — the outage is visible only by reading the flag.
This detector dates the gate against the widest window a live deploy could explain, and
CLEARS it once the convergence that set it is provably gone — falling back to the hard
FAIL (the doctor exit code the watchdog keys on) wherever that proof is unavailable.
"""

import datetime as dt
import io
import pathlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stdout
from unittest import mock

import django.test
from django.utils import timezone

from teatree.cli.doctor import self_heal, self_heal_quiescing
from teatree.cli.doctor.deploy_liveness import DeployLiveness
from teatree.config.resolution import worker_is_quiescing
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


@contextmanager
def _liveness(verdict: DeployLiveness) -> Iterator[None]:
    """Pin what the venue can see about the convergence, so no test reads the real box."""
    with mock.patch.object(self_heal_quiescing, "probe_deploy_liveness", return_value=verdict):
        yield


class StrandedQuiescingCheckTest(django.test.TestCase):
    def setUp(self) -> None:
        # Default every case to the venue that can prove nothing: the repair is then
        # never authorised, so a test that forgets to say what the box could see reads
        # the report-only behaviour rather than the live box's own deploy state.
        unprobeable = mock.patch.object(
            self_heal_quiescing, "probe_deploy_liveness", return_value=DeployLiveness.UNKNOWN
        )
        unprobeable.start()
        self.addCleanup(unprobeable.stop)

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

    def test_a_provably_dead_convergence_is_cleared_not_merely_reported(self) -> None:
        # The one total-admission stall in this module used to be the only detector that
        # printed and left the factory quiesced. On the same evidence it already computes,
        # it now performs the write deploy.sh's own resume_admission would have done.
        set_worker_quiescing(value=True)
        _age_the_gate(self_heal_quiescing.quiescing_deploy_budget_seconds() + 60)

        with _liveness(DeployLiveness.GONE):
            ok, out = _echoes(self_heal_quiescing.check_stranded_quiescing_gate)

        assert worker_is_quiescing() is False, "the detector must resume admission, not describe it"
        assert ok is True, "a healed gate is no longer an outage the watchdog should restart the stack for"
        assert "worker_quiescing" in out
        assert "cleared" in out.lower(), "a repair that does not say it repaired reads as a report"

    def test_a_confirmed_dead_convergence_is_healed_before_the_full_deploy_budget(self) -> None:
        # Age alone is the weakest predicate the box can answer. Once liveness PROVES the
        # convergence is gone, waiting out the remaining stages of a deploy that is not
        # running keeps the factory stalled for nothing.
        set_worker_quiescing(value=True)
        floor = self_heal_quiescing.quiescing_repair_floor_seconds()
        _age_the_gate(floor + 60)

        assert floor < self_heal_quiescing.quiescing_deploy_budget_seconds(), (
            "the confirmed-dead floor exists to heal sooner than the age-alone budget"
        )
        with _liveness(DeployLiveness.GONE):
            ok, _out = _echoes(self_heal_quiescing.check_stranded_quiescing_gate)

        assert worker_is_quiescing() is False
        assert ok is True

    def test_a_venue_that_cannot_probe_reports_and_never_clears(self) -> None:
        # The "could not determine, rendered as nothing is wrong" class, inverted: what it
        # cannot establish it refuses, so the gate survives and the FAIL still pages.
        set_worker_quiescing(value=True)
        _age_the_gate(self_heal_quiescing.quiescing_deploy_budget_seconds() + 60)

        with _liveness(DeployLiveness.UNKNOWN):
            ok, out = _echoes(self_heal_quiescing.check_stranded_quiescing_gate)

        assert worker_is_quiescing() is True, "an unprobeable venue must never guess the deploy is dead"
        assert ok is False
        assert "FAIL" in out
        assert "config_setting set worker_quiescing false" in out

    def test_a_live_convergence_is_never_cleared_under_the_agent(self) -> None:
        set_worker_quiescing(value=True)
        _age_the_gate(self_heal_quiescing.quiescing_deploy_budget_seconds() + 60)

        with _liveness(DeployLiveness.LIVE):
            ok, out = _echoes(self_heal_quiescing.check_stranded_quiescing_gate)

        assert worker_is_quiescing() is True, "clearing the gate mid-drain makes the drain unable to converge"
        assert ok is False
        assert "FAIL" in out

    def test_a_confirmed_dead_convergence_inside_the_floor_is_still_left_alone(self) -> None:
        # The floor is what a deliberate operator pause gets before the doctor undoes it,
        # so a gate set a minute ago survives however dead the last convergence is.
        set_worker_quiescing(value=True)
        _age_the_gate(60)

        assert self_heal_quiescing.quiescing_repair_floor_seconds() > 60, (
            "a floor a fresh gate already clears is no window at all"
        )

        with _liveness(DeployLiveness.GONE):
            ok, out = _echoes(self_heal_quiescing.check_stranded_quiescing_gate)

        assert worker_is_quiescing() is True
        assert ok is True
        assert out == ""

    def test_a_clear_that_does_not_read_back_is_reported_never_claimed(self) -> None:
        # An env layer outranks the config store, so the write lands and changes nothing.
        # Reporting that as a heal would retire the finding while the factory stays stalled.
        set_worker_quiescing(value=True)
        _age_the_gate(self_heal_quiescing.quiescing_deploy_budget_seconds() + 60)

        with (
            _liveness(DeployLiveness.GONE),
            mock.patch.dict("os.environ", {"T3_WORKER_QUIESCING": "1"}),
        ):
            ok, out = _echoes(self_heal_quiescing.check_stranded_quiescing_gate)

        assert ok is False
        assert "FAIL" in out
        assert "config_setting set worker_quiescing false" in out

    def test_a_repair_that_raises_degrades_to_the_report(self) -> None:
        set_worker_quiescing(value=True)
        _age_the_gate(self_heal_quiescing.quiescing_deploy_budget_seconds() + 60)

        with (
            _liveness(DeployLiveness.GONE),
            mock.patch("teatree.loop.drain.set_worker_quiescing", side_effect=RuntimeError("boom")),
        ):
            ok, out = _echoes(self_heal_quiescing.check_stranded_quiescing_gate)

        assert ok is False, "a failed heal leaves the factory stalled, which is a FAIL, not a pass"
        assert "FAIL" in out
        assert worker_is_quiescing() is True

    def test_the_repair_line_is_not_swallowed_by_the_watchdogs_deploy_sensitive_gating(self) -> None:
        set_worker_quiescing(value=True)
        _age_the_gate(self_heal_quiescing.quiescing_deploy_budget_seconds() + 60)

        with _liveness(DeployLiveness.GONE):
            _ok, out = _echoes(self_heal_quiescing.check_stranded_quiescing_gate)

        for gated in ("no loop worker holds the flock", "slack-listener receiver is DOWN", "commit(s) behind origin/"):
            assert gated not in out

    def test_every_bounded_stage_deploy_sh_runs_in_the_window_is_budgeted(self) -> None:
        # The budget tracks deploy.sh's contract, so drift in either direction — a stage
        # renamed there, or one budgeted here that the deploy never waits on — is a bug.
        deploy_sh = (pathlib.Path(__file__).parents[3] / "deploy" / "deploy.sh").read_text(encoding="utf-8")

        for name, default in self_heal_quiescing._DEPLOY_STAGE_BUDGETS:
            assert f"{name}:-{default}" in deploy_sh, f"{name} default drifted from deploy.sh"
