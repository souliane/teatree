"""``manage.py loop_list`` — LIVE loop status from the DB (#1744).

Integration-first: drives the real ``loop_list`` management command via
``call_command`` against a DB seeded with :class:`MiniLoopMarker` and
:class:`LoopLease` rows, asserting the rendered text and the ``--json`` shape.
The mini-loop registry and ``[loops]`` config are patched to a small stub set
so the assertions don't depend on the real domain loops, and the wall clock is
pinned so countdowns are deterministic.
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

from teatree.core.models.loop_lease import LoopLease
from teatree.core.models.mini_loop_marker import MiniLoopMarker
from teatree.loops.base import MiniLoop
from teatree.loops.config import LoopsConfig

_LIVE_PID = os.getpid()
_DEAD_PID = 2_000_000_000


def _stub_loop(name: str, cadence: int, *, always_on: bool = False) -> MiniLoop:
    return MiniLoop(name=name, default_cadence_seconds=cadence, build_jobs=lambda **_: [], always_on=always_on)


@contextmanager
def _registry(*loops: MiniLoop) -> Iterator[None]:
    with (
        patch("teatree.loops.live.iter_loops", return_value=loops),
        patch.object(LoopsConfig, "load", classmethod(lambda cls, path=None: cls())),
    ):
        yield


def _run(*args: str) -> str:
    out = io.StringIO()
    call_command("loop_list", *args, stdout=out)
    return out.getvalue()


@django.test.override_settings(USE_TZ=True)
class TestLoopListText(django.test.TestCase):
    def test_never_fired_loop_renders_em_dash_next(self) -> None:
        with _registry(_stub_loop("dispatch", 300)):
            output = _run()
        line = next(ln for ln in output.splitlines() if ln.strip().startswith("dispatch"))
        assert "next —" in line
        assert "last —" in line

    def test_overdue_loop_renders_overdue(self) -> None:
        MiniLoopMarker.objects.mark_fired("audit", timezone.now() - dt.timedelta(hours=2))
        with _registry(_stub_loop("audit", 60)):
            output = _run()
        line = next(ln for ln in output.splitlines() if ln.strip().startswith("audit"))
        assert "next overdue" in line

    def test_disabled_loop_shown_with_disabled_marker(self) -> None:
        with (
            _registry(_stub_loop("review", 300)),
            patch.object(LoopsConfig, "is_enabled", lambda self, loop: False),
        ):
            output = _run()
        line = next(ln for ln in output.splitlines() if ln.strip().startswith("review"))
        assert "disabled" in line

    def test_infra_slots_listed_before_mini_loops(self) -> None:
        with _registry(_stub_loop("dispatch", 300)):
            output = _run()
        assert output.index("infra slots:") < output.index("mini-loops:")
        assert "loop-tick" in output

    def test_stall_warning_when_last_tick_old(self) -> None:
        LoopLease.objects.filter(name="loop-tick").delete()
        lease = LoopLease.objects.create(name="loop-tick", owner="t")
        lease.acquired_at = timezone.now() - dt.timedelta(hours=10)
        lease.save(update_fields=["acquired_at"])
        with _registry(_stub_loop("dispatch", 300)):
            output = _run()
        assert "STALLED" in output
        assert "t3 loop tick" in output
        assert "t3 loop claim" in output

    def test_no_stall_when_recent_tick(self) -> None:
        LoopLease.objects.create(name="loop-tick", owner="t", acquired_at=timezone.now())
        MiniLoopMarker.objects.mark_fired("dispatch", timezone.now())
        with _registry(_stub_loop("dispatch", 300)):
            output = _run()
        assert "STALLED" not in output


@django.test.override_settings(USE_TZ=True)
class TestLoopOwnerLine(django.test.TestCase):
    def test_live_owner_pid_reported_alive(self) -> None:
        LoopLease.objects.create(
            name="loop-owner",
            session_id="sess-live",
            owner_pid=_LIVE_PID,
            acquired_at=timezone.now(),
            lease_expires_at=timezone.now() + dt.timedelta(minutes=30),
        )
        with _registry(_stub_loop("dispatch", 300)):
            output = _run()
        line = next(ln for ln in output.splitlines() if ln.startswith("loop-owner:"))
        assert "sess-live" in line
        assert "alive" in line
        assert "live" in line

    def test_dead_owner_pid_reported_dead_and_stale(self) -> None:
        LoopLease.objects.create(
            name="loop-owner",
            session_id="sess-dead",
            owner_pid=_DEAD_PID,
            acquired_at=timezone.now() - dt.timedelta(hours=2),
            lease_expires_at=timezone.now() - dt.timedelta(hours=1),
        )
        with _registry(_stub_loop("dispatch", 300)):
            output = _run()
        line = next(ln for ln in output.splitlines() if ln.startswith("loop-owner:"))
        assert "sess-dead" in line
        assert "dead/unknown" in line
        assert "stale" in line

    def test_unclaimed_owner_reported(self) -> None:
        with _registry(_stub_loop("dispatch", 300)):
            output = _run()
        line = next(ln for ln in output.splitlines() if ln.startswith("loop-owner:"))
        assert "unclaimed" in line


@django.test.override_settings(USE_TZ=True)
class TestLoopListJson(django.test.TestCase):
    def test_json_shape(self) -> None:
        fired = timezone.now() - dt.timedelta(seconds=120)
        MiniLoopMarker.objects.mark_fired("dispatch", fired)
        LoopLease.objects.create(
            name="loop-owner",
            session_id="sess-json",
            owner_pid=_LIVE_PID,
            acquired_at=timezone.now(),
            lease_expires_at=timezone.now() + dt.timedelta(minutes=30),
        )
        with _registry(_stub_loop("dispatch", 300)):
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
        with _registry(_stub_loop("inbox", 60)):
            payload = json.loads(_run("--json"))
        inbox = next(e for e in payload["mini_loops"] if e["name"] == "inbox")
        assert inbox["last_fired_at"] == ""
        assert inbox["next_fire_at"] == ""
        assert inbox["age_seconds"] is None


@django.test.override_settings(USE_TZ=True)
class TestLoopListIsReadOnly(django.test.TestCase):
    def test_no_rows_created_or_mutated(self) -> None:
        with _registry(_stub_loop("dispatch", 300)):
            _run()
            _run("--json")
            _run("--all")
        assert MiniLoopMarker.objects.count() == 0
        assert not LoopLease.objects.exclude(session_id="").exists()


@django.test.override_settings(USE_TZ=True)
class TestLoopListAllPerLoopOwners(django.test.TestCase):
    """``t3 loop list --all`` — the cross-session per-loop owner health view (#1834)."""

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

    def test_default_view_omits_per_loop_block(self) -> None:
        self._seed_per_loop_owners()
        with _registry(_stub_loop("dispatch", 300)):
            output = _run()
        assert "per-loop owners:" not in output
        assert "loop:dispatch" not in output

    def test_all_renders_each_per_loop_owner(self) -> None:
        self._seed_per_loop_owners()
        with _registry(_stub_loop("dispatch", 300)):
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
        with _registry(_stub_loop("dispatch", 300)):
            output = _run("--all")
        assert "per-loop owners:" not in output

    def test_default_text_byte_identical_with_and_without_per_loop_rows(self) -> None:
        """The single-owner default text is unchanged whether per-loop rows exist."""
        with _registry(_stub_loop("dispatch", 300)):
            before = _run()
        self._seed_per_loop_owners()
        with _registry(_stub_loop("dispatch", 300)):
            after = _run()
        assert before == after

    def test_all_json_includes_per_loop_owners(self) -> None:
        self._seed_per_loop_owners()
        with _registry(_stub_loop("dispatch", 300)):
            payload = json.loads(_run("--all", "--json"))
        assert "per_loop_owners" in payload
        slots = {o["slot"] for o in payload["per_loop_owners"]}
        assert slots == {"loop:dispatch", "loop:review"}
        dispatch = next(o for o in payload["per_loop_owners"] if o["slot"] == "loop:dispatch")
        assert dispatch["session_id"] == "sess-dispatch"
        assert dispatch["pid_is_alive"] is True
        assert dispatch["is_live"] is True

    def test_default_json_owner_block_byte_identical(self) -> None:
        """Without ``--all`` the ``owner`` JSON block keeps its #1744 shape (no per_loop_owners)."""
        self._seed_per_loop_owners()
        LoopLease.objects.create(
            name="loop-owner",
            session_id="sess-global",
            owner_pid=_LIVE_PID,
            acquired_at=timezone.now(),
            lease_expires_at=timezone.now() + dt.timedelta(minutes=30),
        )
        with _registry(_stub_loop("dispatch", 300)):
            payload = json.loads(_run("--json"))
        assert "per_loop_owners" not in payload
        assert set(payload["owner"].keys()) == {"session_id", "owner_pid", "pid_is_alive", "is_live"}
