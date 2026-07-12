"""``manage.py loop_list`` — LIVE loop status from the DB (#1744).

Integration-first: drives the real ``loop_list`` management command via
``call_command`` against a DB seeded with :class:`Loop` and :class:`LoopLease`
rows, asserting the rendered text and the ``--json`` shape. After the #2513
cutover the mini-loop rows come from the DB ``Loop`` table, so the tests that
assert a specific mini-loop's rendering clear the seeded production loops
(migration 0078) and create their own rows; the wall clock is anchored on
``last_run_at`` so countdowns are deterministic.
"""

import datetime as dt
import io
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import django.test
from django.core.management import call_command
from django.utils import timezone

from teatree.core.models import Loop, LoopPreset, LoopPresetOverride, LoopState, Prompt
from teatree.core.models.loop_lease import LoopLease

_LIVE_PID = os.getpid()
_DEAD_PID = 2_000_000_000


def _prompt() -> Prompt:
    prompt, _ = Prompt.objects.get_or_create(name="demo-loop-list", defaults={"body": "x"})
    return prompt


def _make_loop(name: str, cadence: int, *, last_run_at: dt.datetime | None = None, enabled: bool = True) -> Loop:
    return Loop.objects.create(
        name=name,
        delay_seconds=cadence,
        prompt=_prompt(),
        enabled=enabled,
        last_run_at=last_run_at,
    )


def _run(*args: str) -> str:
    out = io.StringIO()
    call_command("loop_list", *args, stdout=out)
    return out.getvalue()


@django.test.override_settings(USE_TZ=True)
class TestLoopListText(django.test.TestCase):
    def test_never_fired_loop_renders_em_dash_next(self) -> None:
        Loop.objects.all().delete()
        _make_loop("dispatch", 300)
        output = _run()
        # Each loop is a table row on one line; a never-fired loop shows "—"
        # in both its Last and Next cells.
        line = next(ln for ln in output.splitlines() if "dispatch" in ln)
        assert line.count("—") >= 2

    def test_overdue_loop_renders_overdue(self) -> None:
        Loop.objects.all().delete()
        _make_loop("audit", 60, last_run_at=timezone.now() - dt.timedelta(hours=2))
        output = _run()
        line = next(ln for ln in output.splitlines() if "audit" in ln)
        assert "overdue" in line

    def test_disabled_loop_shown_with_disabled_marker(self) -> None:
        Loop.objects.all().delete()
        _make_loop("review", 300, enabled=False)
        output = _run()
        line = next(ln for ln in output.splitlines() if "review" in ln)
        assert "disabled" in line

    def test_infra_slots_listed_before_mini_loops(self) -> None:
        output = _run()
        assert output.index("infra slots") < output.index("mini-loops")
        assert "loop-tick" in output

    def test_paused_loop_shows_held_marker_despite_enabled_row(self) -> None:
        # A PAUSED loop keeps Loop.enabled=True with a live countdown; the
        # `held` marker is the only signal that the tick will skip it.
        Loop.objects.all().delete()
        _make_loop("review", 300, last_run_at=timezone.now())
        LoopState.objects.pause("review")
        output = _run()
        line = next(ln for ln in output.splitlines() if "review" in ln)
        assert "held" in line
        assert "enabled" in line

    def test_stall_warning_when_last_tick_old(self) -> None:
        # Every Loop row never ran (no mini-loop contributes a recent tick) and
        # the only infra lease was acquired 10h ago ⇒ last_tick is stale.
        Loop.objects.update(last_run_at=None)
        LoopLease.objects.filter(name="loop-tick").delete()
        lease = LoopLease.objects.create(name="loop-tick", owner="t")
        lease.acquired_at = timezone.now() - dt.timedelta(hours=10)
        lease.save(update_fields=["acquired_at"])
        output = _run()
        assert "STALLED" in output
        # #2650 remedy: re-register the per-loop `/loop`s via `/t3:loops`, or take
        # ownership with `t3 loop claim`; the human force-render is the PLURAL
        # `t3 loops tick` — never the retired singular `t3 loop tick` shim.
        assert "/t3:loops" in output
        assert "t3 loops tick" in output
        assert "t3 loop claim" in output

    def test_no_stall_when_recent_tick(self) -> None:
        # A recent infra tick is enough to clear the stall — the mini-loop
        # source no longer matters for ``last_tick_at``.
        LoopLease.objects.create(name="loop-tick", owner="t", acquired_at=timezone.now())
        output = _run()
        assert "STALLED" not in output


@django.test.override_settings(USE_TZ=True)
class TestLoopOwnerLine(django.test.TestCase):
    def test_live_owner_pid_reported_alive(self) -> None:
        LoopLease.objects.create(
            name="t3-master",
            session_id="sess-live",
            owner_pid=_LIVE_PID,
            acquired_at=timezone.now(),
            lease_expires_at=timezone.now() + dt.timedelta(minutes=30),
        )
        output = _run()
        line = next(ln for ln in output.splitlines() if ln.startswith("t3-master:"))
        assert "sess-live" in line
        assert "alive" in line
        assert "live" in line

    def test_dead_owner_pid_reported_dead_and_stale(self) -> None:
        LoopLease.objects.create(
            name="t3-master",
            session_id="sess-dead",
            owner_pid=_DEAD_PID,
            acquired_at=timezone.now() - dt.timedelta(hours=2),
            lease_expires_at=timezone.now() - dt.timedelta(hours=1),
        )
        output = _run()
        line = next(ln for ln in output.splitlines() if ln.startswith("t3-master:"))
        assert "sess-dead" in line
        assert "dead/unknown" in line
        assert "stale" in line

    def test_unclaimed_owner_reported(self) -> None:
        output = _run()
        line = next(ln for ln in output.splitlines() if ln.startswith("t3-master:"))
        assert "unclaimed" in line


@django.test.override_settings(USE_TZ=True)
class TestLoopListJson(django.test.TestCase):
    def test_json_shape(self) -> None:
        Loop.objects.all().delete()
        fired = timezone.now() - dt.timedelta(seconds=120)
        _make_loop("dispatch", 300, last_run_at=fired)
        LoopLease.objects.create(
            name="t3-master",
            session_id="sess-json",
            owner_pid=_LIVE_PID,
            acquired_at=timezone.now(),
            lease_expires_at=timezone.now() + dt.timedelta(minutes=30),
        )
        payload = json.loads(_run("--json"))
        assert {"infra_slots", "mini_loops", "owner", "stalled", "tick_cadence_seconds"} <= payload.keys()
        dispatch = next(e for e in payload["mini_loops"] if e["name"] == "dispatch")
        assert dispatch["kind"] == "mini-loop"
        assert dispatch["enabled"] is True
        assert dispatch["never_fired"] is False
        assert payload["owner"]["session_id"] == "sess-json"
        assert payload["owner"]["pid_is_alive"] is True
        infra_names = {e["name"] for e in payload["infra_slots"]}
        assert "loop-tick" in infra_names

    def test_json_never_fired_has_empty_timestamps(self) -> None:
        Loop.objects.all().delete()
        _make_loop("inbox", 60)
        payload = json.loads(_run("--json"))
        inbox = next(e for e in payload["mini_loops"] if e["name"] == "inbox")
        assert inbox["last_fired_at"] == ""
        assert inbox["next_fire_at"] == ""
        assert inbox["age_seconds"] is None

    def test_json_paused_loop_reports_held_true(self) -> None:
        Loop.objects.all().delete()
        _make_loop("review", 300, last_run_at=timezone.now())
        LoopState.objects.pause("review")
        payload = json.loads(_run("--json"))
        review = next(e for e in payload["mini_loops"] if e["name"] == "review")
        assert review["held"] is True
        assert review["enabled"] is True

    def test_json_running_loop_reports_held_false(self) -> None:
        Loop.objects.all().delete()
        _make_loop("dispatch", 300, last_run_at=timezone.now())
        payload = json.loads(_run("--json"))
        dispatch = next(e for e in payload["mini_loops"] if e["name"] == "dispatch")
        assert dispatch["held"] is False


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestLoopListReflectsPresetMask(django.test.TestCase):
    """#3159: the live view surfaces a preset mask, not just enabled+held.

    A preset can mask a base-enabled loop OFF (or force a base-disabled loop ON)
    with no ``LoopState`` hold, so the ``Held`` signal column — "will this tick" —
    must reflect it instead of leaving a masked loop reading plainly ``enabled``.
    """

    def _activate(self, preset_name: str, entries: dict[str, bool]) -> None:
        LoopPreset.objects.create(name=preset_name, entries=entries)
        LoopPresetOverride.objects.set_override(preset_name)

    def test_masked_off_loop_shows_masked(self) -> None:
        Loop.objects.all().delete()
        _make_loop("review", 300, last_run_at=timezone.now())
        self._activate("heads-down", {"review": False})
        line = next(ln for ln in _run().splitlines() if "review" in ln)
        assert "masked" in line

    def test_forced_on_base_disabled_loop_shows_forced_on(self) -> None:
        Loop.objects.all().delete()
        _make_loop("audit", 300, last_run_at=timezone.now(), enabled=False)
        self._activate("engaged", {"audit": True})
        line = next(ln for ln in _run().splitlines() if "audit" in ln)
        assert "forced-on" in line

    def test_plain_disabled_loop_is_not_labelled_masked(self) -> None:
        Loop.objects.all().delete()
        _make_loop("inbox", 300, enabled=False)
        line = next(ln for ln in _run().splitlines() if "inbox" in ln)
        assert "masked" not in line
        assert "disabled" in line

    def test_json_carries_admitted_verdict(self) -> None:
        Loop.objects.all().delete()
        _make_loop("review", 300, last_run_at=timezone.now())
        self._activate("heads-down", {"review": False})
        review = next(e for e in json.loads(_run("--json"))["mini_loops"] if e["name"] == "review")
        assert review["admitted"] is False
        assert review["enabled"] is True


@django.test.override_settings(USE_TZ=True)
class TestLoopListIsReadOnly(django.test.TestCase):
    def test_no_rows_created_or_mutated(self) -> None:
        loop_count_before = Loop.objects.count()
        _run()
        _run("--json")
        _run("--all")
        assert Loop.objects.count() == loop_count_before
        assert not LoopLease.objects.exclude(session_id="").exists()


@contextmanager
def _session(session_id: str) -> Iterator[None]:
    """Pin the session id the default-view scoping reads (#1834 WI-2)."""
    with patch("teatree.core.management.commands.loop_list.current_session_id", return_value=session_id):
        yield


@django.test.override_settings(USE_TZ=True)
class TestLoopListPerLoopOwners(django.test.TestCase):
    """``t3 loop list`` per-loop owner views — scoped default vs ``--all`` (#1834).

    WI-2: the DEFAULT view scopes the per-loop block to the CURRENT session's
    owned loops; ``--all`` stays the cross-session health view. The
    single-owner default (no ``loop:<name>`` lease) short-circuits to today's
    byte-identical output.
    """

    def _seed_per_loop_owners(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(
            name="loop:dispatch",
            session_id="sess-dispatch",
            owner_pid=_LIVE_PID,
            acquired_at=now,
            lease_expires_at=now + dt.timedelta(minutes=30),
        )
        LoopLease.objects.create(
            name="loop:review",
            session_id="sess-review",
            owner_pid=_DEAD_PID,
            acquired_at=now - dt.timedelta(hours=2),
            lease_expires_at=now - dt.timedelta(hours=1),
        )

    def test_default_view_scopes_to_current_session(self) -> None:
        """Session A sees only its own loop by default; B's loop is subtracted."""
        self._seed_per_loop_owners()
        with _session("sess-dispatch"):
            output = _run()
        assert "per-loop owners:" in output
        assert "loop:dispatch" in output
        assert "loop:review" not in output

    def test_all_shows_both_sessions_proving_default_subtracted(self) -> None:
        """``--all`` lists B's loop too — proving the default actually subtracted it."""
        self._seed_per_loop_owners()
        with _session("sess-dispatch"):
            default_output = _run()
            all_output = _run("--all")
        assert "loop:review" not in default_output
        # The cross-session view CONTAINS B's loop — the row existed, the
        # default filter removed it (not a "B never existed" false pass).
        assert "loop:dispatch" in all_output
        assert "loop:review" in all_output

    def test_empty_session_default_shows_full_view(self) -> None:
        """A cron / anonymous tick (no session) fails open to the full view, never empty."""
        self._seed_per_loop_owners()
        with _session(""):
            output = _run()
        assert "per-loop owners:" in output
        assert "loop:dispatch" in output
        assert "loop:review" in output

    def test_all_renders_each_per_loop_owner(self) -> None:
        self._seed_per_loop_owners()
        with _session("sess-dispatch"):
            output = _run("--all")
        assert "per-loop owners:" in output
        dispatch_line = next(ln for ln in output.splitlines() if "loop:dispatch" in ln)
        assert "sess-dispatch" in dispatch_line
        assert "alive" in dispatch_line
        assert "live" in dispatch_line
        review_line = next(ln for ln in output.splitlines() if "loop:review" in ln)
        assert "sess-review" in review_line
        assert "dead/unknown" in review_line
        assert "stale" in review_line

    def test_all_with_no_per_loop_owners_shows_no_block(self) -> None:
        with _session("sess-dispatch"):
            output = _run("--all")
        assert "per-loop owners:" not in output

    def test_single_owner_default_byte_identical_to_today(self) -> None:
        """No ``loop:<name>`` lease present ⇒ default output unchanged.

        The load-bearing anti-regression: with no per-loop lease present the
        default view must be byte-identical whether or not a current session
        resolves — the per-loop block is absent in both cases.

        The clock is frozen across both renders: ``build_report`` anchors every
        countdown on ``timezone.now()`` at call time, so a sub-hour ``next in
        Xm YYs`` countdown (a ``daily_at`` loop seeded by migration 0078) ticks
        a second between the two ``_run()`` calls and would make the output
        differ on the *time*, not on the session-scoping invariant under test.
        Pinning ``now`` isolates the assertion to the behaviour it guards.
        """
        with patch("teatree.loops.live.timezone.now", return_value=timezone.now()):
            with _session("sess-dispatch"):
                with_session = _run()
            with _session(""):
                anonymous = _run()
        assert with_session == anonymous
        assert "per-loop owners:" not in with_session

    def test_all_json_includes_per_loop_owners(self) -> None:
        self._seed_per_loop_owners()
        with _session("sess-dispatch"):
            payload = json.loads(_run("--all", "--json"))
        assert "per_loop_owners" in payload
        slots = {o["slot"] for o in payload["per_loop_owners"]}
        assert slots == {"loop:dispatch", "loop:review"}
        dispatch = next(o for o in payload["per_loop_owners"] if o["slot"] == "loop:dispatch")
        assert dispatch["session_id"] == "sess-dispatch"
        assert dispatch["pid_is_alive"] is True
        assert dispatch["is_live"] is True

    def test_default_json_scopes_per_loop_owners_to_session(self) -> None:
        """The default ``--json`` per_loop_owners block is scoped to the current session."""
        self._seed_per_loop_owners()
        with _session("sess-dispatch"):
            payload = json.loads(_run("--json"))
        assert {o["slot"] for o in payload["per_loop_owners"]} == {"loop:dispatch"}

    def test_default_json_byte_identical_to_today_when_no_per_loop_rows(self) -> None:
        """With no ``loop:<name>`` lease the default ``--json`` keeps its #1744 shape.

        The ``owner`` block stays exactly the #1744 keys and no
        ``per_loop_owners`` key is added — byte-identical to today.
        """
        LoopLease.objects.create(
            name="t3-master",
            session_id="sess-global",
            owner_pid=_LIVE_PID,
            acquired_at=timezone.now(),
            lease_expires_at=timezone.now() + dt.timedelta(minutes=30),
        )
        with _session("sess-dispatch"):
            payload = json.loads(_run("--json"))
        assert "per_loop_owners" not in payload
        assert set(payload["owner"].keys()) == {"session_id", "owner_pid", "pid_is_alive", "is_live"}
