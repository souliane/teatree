"""``manage.py loops_tick`` — the single tick surface (#1796 / #2777 cutover).

Drives the real command via ``call_command`` with the dispatch pipeline and
backends mocked. Asserts the singleton ``loop-owner`` ownership gate (non-owner
SKIPs) and that the won path runs ``run_tick`` with the DB-``Loop``-driven jobs
builder. After #2777 the bare master claims ``loop-owner`` + ``loop-tick`` (the
slots the retired ``loop_tick`` command held), so the self-pump cutover is
behaviour-preserving.
"""

import datetime as dt
import io
from unittest.mock import patch

import django.test
import pytest
from django.core.management import call_command

from teatree.core.management.commands.loops_tick import _loop_table_jobs_builder
from teatree.core.models import Loop, LoopLease, Worktree
from teatree.core.overlay import OverlayBase, ProvisionStep
from teatree.loop.tick import TickReport


def _run(**kwargs: object) -> str:
    out = io.StringIO()
    call_command("loops_tick", stdout=out, **kwargs)
    return out.getvalue()


class _CleanOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        _ = worktree
        return []


class _SlackDownOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        _ = worktree
        return []

    def get_connector_preflight(self) -> list:
        def _probe() -> None:
            msg = "Slack auth.test failed: missing_scope"
            raise RuntimeError(msg)

        return [_probe]


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

    def test_bare_master_claims_loop_owner_and_loop_tick_never_t3_master(self) -> None:
        """#2777 L1: bare master claims the unified ``loop-owner`` + ``loop-tick`` slots.

        RED on main: the bare master claimed ``t3-master`` + ``t3-master-tick`` (a
        slot the live session's lease + the self-pump cutover did not share), so no
        ``loop-owner`` row was written.
        """
        report = TickReport(started_at=dt.datetime.now(dt.UTC))
        with (
            patch.dict("os.environ", {"CLAUDE_SESSION_ID": "owner-session"}),
            patch("teatree.core.connector_preflight.run_connector_preflight"),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
            patch("teatree.loop.tick_piggyback.run_piggyback_cycles"),
        ):
            _run()
        assert LoopLease.objects.get(name="loop-owner").session_id == "owner-session"
        # The per-tick mutex row exists (acquired then released → owner blanked).
        assert LoopLease.objects.filter(name="loop-tick").exists()
        assert not LoopLease.objects.filter(name="t3-master").exists()
        assert not LoopLease.objects.filter(name="t3-master-tick").exists()

    def test_master_skip_names_the_loop_owner_slot(self) -> None:
        """#2777 L2: the SKIP remedy interpolates the REAL slot (``loop-owner``)."""
        with (
            patch("teatree.core.connector_preflight.run_connector_preflight"),
            patch.object(LoopLease.objects, "claim_ownership", return_value=(False, "other-session")),
            patch("teatree.loop.tick.run_tick") as run_tick,
        ):
            out = _run()
        assert "t3 loop claim --slot loop-owner --take-over" in out
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
            patch("teatree.loop.tick_piggyback.run_piggyback_cycles") as piggyback,
        ):
            _run()
        assert run_tick.called
        assert run_tick.call_args.kwargs["jobs_builder"] is _loop_table_jobs_builder
        # The full master fan-out also runs the won-tick reactive piggyback cycles.
        piggyback.assert_called_once()


class TestLoopsTickPerLoop(django.test.TestCase):
    """``t3 loops tick --loop <name>`` — one enabled DB Loop per native ``/loop`` (#2650)."""

    def test_loop_flag_claims_the_per_loop_lease(self) -> None:
        report = TickReport(started_at=dt.datetime.now(dt.UTC))
        captured: dict[str, str] = {}

        def _claim(slot: str, **_: object) -> tuple[bool, str]:
            captured["slot"] = slot
            return (True, "me")

        with (
            patch("teatree.core.connector_preflight.run_connector_preflight"),
            patch.object(LoopLease.objects, "claim_ownership", side_effect=_claim),
            patch.object(LoopLease.objects, "acquire", return_value=True),
            patch.object(LoopLease.objects, "release"),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
            patch("teatree.loop.tick_piggyback.run_piggyback_cycles") as piggyback,
        ):
            _run(loop="inbox")
        # A disjoint per-loop owner key (``loop:<name>``), never the singleton
        # ``loop-owner`` — so the N per-loop ``/loop``s run in parallel, not
        # serialised on one master lease.
        assert captured["slot"] == "loop:inbox"
        # The reactive piggyback cycles belong to the master fan-out, NOT a
        # single-loop tick — never amplified once per enabled loop.
        piggyback.assert_not_called()

    def test_per_loop_skip_names_the_real_per_loop_slot(self) -> None:
        """#2777 L2: a per-loop SKIP interpolates the per-loop ``loop:<name>`` slot.

        RED on main: the remedy was the bare ``t3 loop claim --take-over`` (the
        wrong slot — a per-loop hand-off needs ``--slot loop:dispatch``).
        """
        with (
            patch("teatree.core.connector_preflight.run_connector_preflight"),
            patch.object(LoopLease.objects, "claim_ownership", return_value=(False, "other-session")),
            patch("teatree.loop.tick.run_tick") as run_tick,
        ):
            out = _run(loop="dispatch")
        assert "t3 loop claim --slot loop:dispatch --take-over" in out
        run_tick.assert_not_called()

    def test_loop_flag_scopes_the_jobs_builder_to_that_one_loop(self) -> None:
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
            _run(loop="inbox")
        from teatree.loop.tick import TickRequest  # noqa: PLC0415

        jobs_builder = run_tick.call_args.kwargs["jobs_builder"]
        assert jobs_builder is not _loop_table_jobs_builder
        with patch("teatree.loops.master.build_loop_table_jobs", return_value=[]) as build:
            jobs_builder(TickRequest(), dt.datetime.now(dt.UTC))
        assert build.call_args.kwargs["only"] == "inbox"


class TestPerLoopConnectorIsolation(django.test.TestCase):
    """A per-loop tick preflights ONLY its own overlay — one outage can't take the fleet (LOOP-PR-C).

    The bug: ``t3 loops tick --loop X`` (no ``--overlay``) ran the fleet-wide
    ``run_connector_preflight("")`` BEFORE the enabled/due gate, so an unrelated
    overlay's connector outage ``SystemExit``-ed loop ``X``'s tick — one outage
    SystemExits the whole fleet of per-loop loops.
    """

    @staticmethod
    def _seed(name: str, overlay: str, *, enabled: bool = True, last_run_at: dt.datetime | None = None) -> None:
        Loop.objects.create(
            name=name,
            script=f"src/teatree/loops/{name}/loop.py",
            delay_seconds=60,
            overlay=overlay,
            enabled=enabled,
            last_run_at=last_run_at,
        )

    def _run_isolated(self, *, loop: str) -> object:
        report = TickReport(started_at=dt.datetime.now(dt.UTC))
        overlays = {"alpha": _CleanOverlay(), "beta": _SlackDownOverlay()}
        with (
            patch("teatree.core.connector_preflight.get_all_overlays", return_value=overlays),
            patch("teatree.core.overlay_loader.get_all_overlay_names", return_value=list(overlays)),
            patch.object(LoopLease.objects, "claim_ownership", return_value=(True, "me")),
            patch.object(LoopLease.objects, "acquire", return_value=True),
            patch.object(LoopLease.objects, "release"),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report) as run_tick,
            patch("teatree.loop.tick_piggyback.run_piggyback_cycles"),
        ):
            _run(loop=loop)
        return run_tick

    def test_unrelated_overlay_outage_does_not_systemexit_per_loop_tick(self) -> None:
        self._seed("probe-alpha", overlay="alpha")
        run_tick = self._run_isolated(loop="probe-alpha")
        assert run_tick.called

    def test_disabled_loop_on_down_overlay_does_not_systemexit(self) -> None:
        self._seed("probe-beta", overlay="beta", enabled=False)
        run_tick = self._run_isolated(loop="probe-beta")
        assert run_tick.called

    def test_loop_on_own_down_overlay_still_systemexits(self) -> None:
        self._seed("probe-beta", overlay="beta")
        with pytest.raises(SystemExit):
            self._run_isolated(loop="probe-beta")
