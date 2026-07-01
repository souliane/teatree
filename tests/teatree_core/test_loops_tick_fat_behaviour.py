"""Tests for the ``loops_tick`` Django management command."""

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands.loops_tick import _report_to_dict
from teatree.loop.dispatch import DispatchAction
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.tick import TickReport
from teatree.types import RawAPIDict


def _build_report(*, statusline_path: Path | None = None, errors: dict[str, str] | None = None) -> TickReport:
    return TickReport(
        started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        signals=[ScanSignal(kind="my_pr.open", summary="x")],
        actions=[DispatchAction(kind="statusline", zone="in_flight", detail="x")],
        statusline_path=statusline_path,
        errors=errors or {},
    )


class TestReportToDict(TestCase):
    def test_serialises_full_report(self) -> None:
        report = _build_report(statusline_path=Path("/tmp/sl.txt"), errors={"my_prs": "boom"})

        data = _report_to_dict(report)

        assert data["started_at"] == "2026-01-01T00:00:00+00:00"
        assert data["signal_count"] == 1
        assert data["action_count"] == 1
        assert data["statusline_path"].endswith("sl.txt")
        assert data["errors"] == {"my_prs": "boom"}
        assert data["actions"][0]["zone"] == "in_flight"

    def test_empty_statusline_path_serialises_to_empty_string(self) -> None:
        report = _build_report(statusline_path=None)

        data = _report_to_dict(report)

        assert data["statusline_path"] == ""


class TestLoopTickCommand(TestCase):
    def test_quiet_exit_when_no_errors(self) -> None:
        report = _build_report(statusline_path=Path("/tmp/sl.txt"))
        stdout = StringIO()
        with (
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            call_command("loops_tick", stdout=stdout)

        assert stdout.getvalue() == ""

    def test_text_output_with_errors(self) -> None:
        report = _build_report(statusline_path=Path("/tmp/sl.txt"), errors={"my_prs": "RuntimeError: x"})
        stdout = StringIO()
        with (
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            call_command("loops_tick", stdout=stdout)

        output = stdout.getvalue()
        assert "WARN  my_prs" in output

    def test_drains_deferred_reinstall_before_running_scanners(self) -> None:
        """#1760: the deferred-reinstall drain is the FIRST owner-tick step.

        It must run before ``run_tick`` (the scanner pass) so the reinstall
        happens in a process that has not yet imported the about-to-change
        scanner code (no mixed-code window).
        """
        report = _build_report()
        order: list[str] = []
        with (
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch(
                "teatree.loop.self_update_reinstall.drain_pending_reinstall",
                side_effect=lambda: order.append("drain"),
            ),
            patch("teatree.loop.tick.run_tick", side_effect=lambda *a, **k: (order.append("tick"), report)[1]),
        ):
            call_command("loops_tick", stdout=StringIO())

        assert order == ["drain", "tick"], "drain must precede the scanner pass"

    def test_installs_then_resets_mini_loop_schedules_reader(self) -> None:
        from unittest.mock import call  # noqa: PLC0415

        from teatree.loops.schedule import mini_loop_schedules  # noqa: PLC0415

        report = _build_report(statusline_path=Path("/tmp/sl.txt"))
        with (
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
            patch("teatree.loop.statusline.set_mini_loop_schedules_reader") as seam,
        ):
            call_command("loops_tick", stdout=StringIO())
        # Installed for the tick, then reset so the process-global never leaks.
        assert seam.call_args_list == [call(mini_loop_schedules), call(None)]

    def test_text_output_includes_scanner_errors(self) -> None:
        report = _build_report(errors={"my_prs": "RuntimeError: x"})
        stdout = StringIO()
        with (
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            call_command("loops_tick", stdout=stdout)

        output = stdout.getvalue()
        assert "WARN  my_prs" in output

    def test_json_output(self) -> None:
        report = _build_report(statusline_path=Path("/tmp/sl.txt"))
        stdout = StringIO()
        with (
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            call_command("loops_tick", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        assert payload["signal_count"] == 1
        assert payload["action_count"] == 1

    def test_overlay_option_uses_single_overlay_path(self) -> None:
        report = _build_report()
        stdout = StringIO()
        with (
            patch("teatree.core.management.commands.loops_tick.code_host_from_overlay", return_value=None) as host_mock,
            patch("teatree.core.management.commands.loops_tick.messaging_from_overlay", return_value=None),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            call_command("loops_tick", "--overlay", "myoverlay", stdout=stdout)

        host_mock.assert_called_once()

    def test_connector_preflight_aborts_tick_before_running(self) -> None:
        """A down connector ``raise SystemExit`` before any tick work.

        Guards the user directive: the loop must refuse to continue when
        a hard-dependency connector is unreachable, not degrade into a
        silent no-op tick.
        """
        report = _build_report()
        with (
            patch(
                "teatree.core.management.commands.loops_tick.run_connector_preflight",
                side_effect=SystemExit("Connector preflight failed for overlay 'acme': Slack down"),
            ),
            patch("teatree.loop.tick.run_tick", return_value=report) as run_tick_mock,
            pytest.raises(SystemExit) as excinfo,
        ):
            call_command("loops_tick", stdout=StringIO())

        assert excinfo.value.code != 0
        run_tick_mock.assert_not_called()

    def test_skips_tick_when_lease_held_by_another_owner(self) -> None:
        """A live DB lease held by another owner makes the command skip (#786 WS2).

        No mock of the lease — a genuine concurrent holder is simulated by
        acquiring the real ``loop-tick`` ``LoopLease`` as a rival owner
        first, exercising the exact production CAS-refusal path that
        replaced the flock.
        """
        from teatree.core.models import LoopLease  # noqa: PLC0415

        assert LoopLease.objects.acquire("loop-tick", owner="rival-tick") is True
        stdout = StringIO()
        with patch("teatree.loop.tick.run_tick") as run_tick_mock:
            call_command("loops_tick", stdout=stdout)

        run_tick_mock.assert_not_called()
        output = stdout.getvalue()
        assert "SKIP" in output
        assert "another tick is already running" in output

    def test_skip_json_emits_full_contract_shape(self) -> None:
        """#744 defect 1: a skipped tick's --json must be contract-shaped.

        A coordinator that pumps ``t3 loop tick --json`` and reads
        ``["signal_count"]`` / ``["errors"]`` must not ``KeyError`` on a
        skipped tick (lease held by a sibling). The skip payload carries
        the full contract keys (zeroed) plus an explicit skipped flag.
        """
        import tempfile  # noqa: PLC0415

        from teatree.core.models import LoopLease  # noqa: PLC0415

        assert LoopLease.objects.acquire("loop-tick", owner="rival-tick") is True
        stdout = StringIO()
        with tempfile.TemporaryDirectory() as d:
            # Isolate the tick-meta freshness-touch off the real
            # ~/.local/share path during the test.
            call_command("loops_tick", "--json", "--statusline-file", str(Path(d) / "sl.txt"), stdout=stdout)

        payload = json.loads(stdout.getvalue())
        assert payload["signal_count"] == 0
        assert payload["action_count"] == 0
        assert payload["errors"] == {}
        assert payload["actions"] == []
        assert "started_at" in payload
        assert "statusline_path" in payload
        assert payload["skipped"] is True
        assert "another tick is already running" in payload["skipped_reason"]

    def test_skip_refreshes_tick_meta_so_no_false_stale(self) -> None:
        """#744 defect 2: a skipped tick must keep tick-meta fresh.

        The lease is held by a *sibling* tick keeping the loop alive,
        so a skip must advance ``tick-meta.json``'s ``next_epoch`` —
        otherwise it decays past ``now + 2*cadence`` and the statusline
        renders a false ``tick stale`` under normal multi-session
        contention.
        """
        import datetime as _dt  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        from teatree.core.models import LoopLease  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as d:
            sl = Path(d) / "statusline.txt"
            meta = sl.with_name("tick-meta.json")
            assert LoopLease.objects.acquire("loop-tick", owner="rival-tick") is True
            before = int(_dt.datetime.now(tz=_dt.UTC).timestamp())
            call_command("loops_tick", "--statusline-file", str(sl), stdout=StringIO())

            assert meta.exists(), "skipped tick did not write tick-meta.json — false 'tick stale' will follow"
            payload = json.loads(meta.read_text(encoding="utf-8"))
            assert payload["next_epoch"] >= before, payload


class TestLoopOwnerGate(TestCase):
    """#1073 — the session-scoped t3-master gate is a hard SKIP.

    The pre-#1073 behaviour was: a non-owner ``t3 loop tick`` would still
    run every scanner and merely *find nothing to claim*. That let a
    foreign session (a blog-post session) drain the user's Slack DMs and
    dispatch reviewers before the claim-next no-op. The gate now SKIPs
    BEFORE any scanner/DM-drain/dispatch.
    """

    def test_non_owner_session_skips_full_tick(self) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        # A live owner already holds the persistent t3-master claim.
        LoopLease.objects.claim_ownership("t3-master", session_id="owner-session")
        stdout = StringIO()
        with (
            patch.dict("os.environ", {"CLAUDE_SESSION_ID": "intruder-session"}),
            patch("teatree.loop.tick.run_tick") as run_tick_mock,
            patch("teatree.loop.tick.build_default_jobs") as build_jobs_mock,
            patch("teatree.loop.tick_piggyback.run_piggyback_cycles") as piggyback_mock,
        ):
            call_command("loops_tick", stdout=stdout)

        run_tick_mock.assert_not_called()
        build_jobs_mock.assert_not_called()
        # #1107 Prong B anti-#1073: a non-owner SKIP must NOT piggyback.
        piggyback_mock.assert_not_called()
        output = stdout.getvalue()
        assert "SKIP  loop slot 't3-master' not owned by this session" in output
        assert "owner is session owner-session" in output
        assert "t3 loop claim --slot t3-master --take-over" in output

    def test_non_owner_skip_json_is_contract_shaped(self) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        LoopLease.objects.claim_ownership("t3-master", session_id="owner-session")
        stdout = StringIO()
        with (
            patch.dict("os.environ", {"CLAUDE_SESSION_ID": "intruder-session"}),
            patch("teatree.loop.tick.run_tick"),
        ):
            call_command("loops_tick", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        assert payload["signal_count"] == 0
        assert payload["action_count"] == 0
        assert payload["errors"] == {}
        assert payload["actions"] == []
        assert payload["skipped"] is True
        assert "loop slot 't3-master' not owned by this session" in payload["skipped_reason"]

    def test_non_owner_skip_refreshes_tick_meta(self) -> None:
        import tempfile  # noqa: PLC0415

        from teatree.core.models import LoopLease  # noqa: PLC0415

        LoopLease.objects.claim_ownership("t3-master", session_id="owner-session")
        with tempfile.TemporaryDirectory() as d:
            sl = Path(d) / "statusline.txt"
            meta = sl.with_name("tick-meta.json")
            before = int(dt.datetime.now(tz=dt.UTC).timestamp())
            with patch.dict("os.environ", {"CLAUDE_SESSION_ID": "intruder-session"}):
                call_command("loops_tick", "--statusline-file", str(sl), stdout=StringIO())
            assert meta.exists(), "non-owner skip must keep tick-meta fresh (no false 'tick stale')"
            assert json.loads(meta.read_text(encoding="utf-8"))["next_epoch"] >= before

    def test_owner_session_runs_tick_and_persists_claim(self) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        report = _build_report(statusline_path=Path("/tmp/sl.txt"))
        stdout = StringIO()
        with (
            patch.dict("os.environ", {"CLAUDE_SESSION_ID": "owner-session"}),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report) as run_tick_mock,
            patch("teatree.loop.tick_piggyback.run_piggyback_cycles") as piggyback_mock,
        ):
            call_command("loops_tick", stdout=stdout)

        run_tick_mock.assert_called_once()
        # #1107 Prong B: the won-owner success path fires the piggyback.
        piggyback_mock.assert_called_once()
        row = LoopLease.objects.get(name="t3-master")
        assert row.session_id == "owner-session"
        assert row.lease_expires_at is not None
        # `loop-tick` (the per-tick mutex) is released in the finally;
        # `t3-master` is NEVER released — its TTL is its sole lifecycle.
        assert LoopLease.objects.get(name="loop-tick").owner == ""

    def test_owner_reclaim_bumps_lease_expiry_each_tick(self) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        report = _build_report()
        with (
            patch.dict("os.environ", {"CLAUDE_SESSION_ID": "owner-session"}),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            call_command("loops_tick", stdout=StringIO())
            first = LoopLease.objects.get(name="t3-master").lease_expires_at
            call_command("loops_tick", stdout=StringIO())
            second = LoopLease.objects.get(name="t3-master").lease_expires_at
        assert second >= first

    def test_first_tick_auto_claims_when_unowned(self) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        report = _build_report()
        with (
            patch.dict("os.environ", {"CLAUDE_SESSION_ID": "fresh-session"}),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report) as run_tick_mock,
        ):
            call_command("loops_tick", stdout=StringIO())

        run_tick_mock.assert_called_once()
        assert LoopLease.objects.get(name="t3-master").session_id == "fresh-session"

    def test_take_over_ends_hijack_within_one_tick(self) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        report = _build_report()
        # Hijacker owns the loop and ticks happily.
        LoopLease.objects.claim_ownership("t3-master", session_id="hijacker")
        with (
            patch.dict("os.environ", {"CLAUDE_SESSION_ID": "hijacker"}),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report) as rt1,
        ):
            call_command("loops_tick", stdout=StringIO())
            rt1.assert_called_once()

        # The chat-only user runs `t3 loop claim --take-over` from main.
        won, _ = LoopLease.objects.claim_ownership("t3-master", session_id="main", take_over=True)
        assert won is True

        # The hijacker's very next tick SKIPs — no restart needed.
        stdout = StringIO()
        with (
            patch.dict("os.environ", {"CLAUDE_SESSION_ID": "hijacker"}),
            patch("teatree.loop.tick.run_tick") as rt2,
        ):
            call_command("loops_tick", stdout=stdout)
        rt2.assert_not_called()
        assert "loop slot 't3-master' not owned by this session" in stdout.getvalue()

    def test_anonymous_session_skips_when_live_owner(self) -> None:
        import tempfile  # noqa: PLC0415

        from teatree.core.models import LoopLease  # noqa: PLC0415

        LoopLease.objects.claim_ownership("t3-master", session_id="owner-session")
        stdout = StringIO()
        # Drop ONLY the session-id vars (anonymous session) — never
        # ``clear=True`` the whole environment: that wipes ``$HOME`` and
        # ``default_path()``'s ``Path.home()`` then raises in containers
        # with no fallback home (the Docker test-matrix).
        env = {k: v for k, v in os.environ.items() if k not in {"CLAUDE_SESSION_ID", "T3_LOOP_SESSION_ID"}}
        with (
            tempfile.TemporaryDirectory() as d,
            patch.dict("os.environ", env, clear=True),
            patch("teatree.loop.tick.run_tick") as run_tick_mock,
        ):
            call_command("loops_tick", "--statusline-file", str(Path(d) / "sl.txt"), stdout=stdout)

        run_tick_mock.assert_not_called()
        assert "loop slot 't3-master' not owned by this session" in stdout.getvalue()

    def test_owner_ttl_env_override_is_parsed_defensively(self) -> None:
        from teatree.core.management.commands.loops_tick import _loop_owner_ttl_seconds  # noqa: PLC0415

        with patch.dict("os.environ", {"T3_LOOP_OWNER_TTL": "3600"}):
            assert _loop_owner_ttl_seconds() == 3600
        with patch.dict("os.environ", {"T3_LOOP_OWNER_TTL": "not-a-number"}):
            assert _loop_owner_ttl_seconds() == 1800
        with patch.dict("os.environ", {"T3_LOOP_OWNER_TTL": "  "}):
            assert _loop_owner_ttl_seconds() == 1800
        with patch.dict("os.environ", {"T3_LOOP_OWNER_TTL": "5"}):
            assert _loop_owner_ttl_seconds() == 60
        with patch.dict("os.environ", {}, clear=True):
            assert _loop_owner_ttl_seconds() == 1800


class TestLoopTickClaimsGlobalOwnerAndRunsFatTick(TestCase):
    """``t3 loop tick`` claims the GLOBAL ``t3-master`` slot and drives ``run_tick``.

    LOOP-PR-A removed the #1838 dedicated-loop ``--slot`` scoped path; the only tick
    the command runs is the fat ``t3-master`` tick over ``build_loop_table_jobs``.
    """

    def test_claims_global_owner_and_runs_fat_tick(self) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        report = _build_report()
        with (
            patch.dict("os.environ", {"CLAUDE_SESSION_ID": "owner-session"}),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report) as fat_run_tick,
        ):
            call_command("loops_tick", stdout=StringIO())

        fat_run_tick.assert_called_once()
        assert LoopLease.objects.get(name="t3-master").session_id == "owner-session"
        assert not LoopLease.objects.filter(name__startswith="loop:").exists()

    def test_slot_option_is_gone(self) -> None:
        from django.core.management.base import CommandError  # noqa: PLC0415

        with pytest.raises((CommandError, SystemExit)):
            call_command("loops_tick", "--slot", "dispatch", stdout=StringIO())


class TestLeaseOwnerPidIsDurableSessionNotTickSubprocess(TestCase):
    """The lease ``owner_pid`` must be the persistent session pid (#1706 root cause).

    ``t3 loop tick`` runs inside the Stop self-pump's Bash-tool shell, which
    the harness tears down seconds after the call. Anchoring the lease on
    ``os.getppid()`` (that transient shell) made the pid-liveness check see
    a dead owner within seconds of every tick, collapsing the pid-anchored
    protection back to TTL-only and letting a fresh SessionStart steal a
    busy owner's loop once the TTL lapsed. The tick must instead read the
    durable session pid the SessionStart hook recorded in the loop registry.
    """

    @pytest.fixture(autouse=True)
    def _registry_isolation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        reg_dir = tmp_path / "data"
        reg_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(reg_dir))
        self._reg_path = reg_dir / "loop-registry.json"

    def _write_owner_record(self, *, session_id: str, pid: int) -> None:
        self._reg_path.write_text(
            json.dumps({"t3-loop-tick-owner": {"session_id": session_id, "agent_id": "a", "pid": pid}}),
            encoding="utf-8",
        )

    def test_tick_stores_registry_session_pid_not_getppid(self) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        durable_session_pid = os.getpid()
        self._write_owner_record(session_id="owner-session", pid=durable_session_pid)

        report = _build_report()
        # A pid that is NOT the durable session pid — what ``os.getppid()``
        # of the transient tick subprocess would have stored pre-fix.
        with (
            patch.dict("os.environ", {"CLAUDE_SESSION_ID": "owner-session"}),
            patch("os.getppid", return_value=999999),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            call_command("loops_tick", stdout=StringIO())

        row = LoopLease.objects.get(name="t3-master")
        assert row.session_id == "owner-session"
        assert row.owner_pid == durable_session_pid, (
            "lease must anchor on the durable session pid from the registry, "
            "not os.getppid() of the transient tick subprocess"
        )

    def test_idle_owner_past_ttl_is_not_stealable_after_tick(self) -> None:
        """End-to-end: a busy/idle owner past TTL stays protected (no steal).

        Pre-fix the tick stored the transient shell pid, so once that shell
        died (seconds later) and the TTL lapsed, ``_session_lease_is_live``
        judged the owner dead and a fresh SessionStart claim won. Post-fix
        the lease carries the alive session pid, so the claim is blocked.
        """
        from teatree.core.models import LoopLease  # noqa: PLC0415

        durable_session_pid = os.getpid()
        self._write_owner_record(session_id="owner-session", pid=durable_session_pid)

        report = _build_report()
        with (
            patch.dict("os.environ", {"CLAUDE_SESSION_ID": "owner-session", "T3_LOOP_OWNER_TTL": "60"}),
            patch("os.getppid", return_value=999999),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            call_command("loops_tick", stdout=StringIO())

        # Simulate the owner going busy/idle past the TTL: the lease lapses
        # but the session process (durable_session_pid) is still alive.
        row = LoopLease.objects.get(name="t3-master")
        row.lease_expires_at = dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=120)
        row.save(update_fields=["lease_expires_at"])

        won, current = LoopLease.objects.claim_ownership("t3-master", session_id="fresh-session")
        assert won is False, "HIJACK: a fresh session stole an alive owner's expired-TTL loop"
        assert current == "owner-session"


_HIJACK_MR_URL = "https://gitlab.com/owner/repo/-/merge_requests/77"
_HIJACK_USER = "U0OWNER"
_HIJACK_CHANNEL = "C0HIJACK"
_HIJACK_TS = "1779180558.111111"


@dataclass
class _ReactionSpyBackend:
    """Real-shaped messaging backend whose message references an MR.

    Module-level (not nested in the test) so the regression test stays
    under the ``C901`` complexity ceiling — the backend is plain test
    plumbing, the assertion logic is what the test is about.
    """

    user_id: str = _HIJACK_USER
    react_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_reactions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        return {"text": f"please review {_HIJACK_MR_URL}", "ts": ts, "channel": channel}

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = channel, text, thread_ts
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        _ = channel, ts, text
        return {}

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return ""

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = channel, ts
        return ""

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""


class TestNonOwnerDoesNotDrainReactionsOrDispatchReviewer(TestCase):
    """#1047 + #1078 — the reaction-driven attack surface is owner-gated.

    #1059 registers ``SlackReviewIntentScanner`` inside ``run_tick`` →
    ``build_default_jobs``. That scanner drains ``slack-reactions.jsonl``
    (atomic rename + unlink, destroying the file) and creates a
    ``ReviewAssignment`` row that dispatches ``t3:reviewer``. #1078's
    session-scoped ``t3-master`` gate SKIPs a NON-OWNER session BEFORE
    ``run_tick`` runs, so the scanner never executes for a foreign
    session.

    Anti-vacuity: this exercises the REAL path (no ``run_tick`` /
    ``build_default_jobs`` mock). With ``loops_tick.py`` resolved to
    main's gated ``handle()`` the JSONL survives untouched and no
    ``ReviewAssignment`` row exists. If the merge had taken #1059's
    ungated ``handle()``, ``run_tick`` would run the scanner, the JSONL
    would be drained (file gone) and a row would be created — the two
    asserts below would FAIL. Verified RED→GREEN by temporarily
    reverting ``loops_tick.py`` to the PR's ungated ``handle()``.
    """

    def test_non_owner_does_not_drain_reactions_jsonl_nor_dispatch_reviewer(self) -> None:
        import tempfile  # noqa: PLC0415

        from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
        from teatree.core.models import LoopLease  # noqa: PLC0415
        from teatree.core.models.review_assignment import ReviewAssignment  # noqa: PLC0415

        backend = _ReactionSpyBackend()
        overlay_backends = [OverlayBackends(name="hijacked", messaging=backend)]

        # A different session legitimately owns the loop.
        LoopLease.objects.claim_ownership("t3-master", session_id="owner-session")

        with tempfile.TemporaryDirectory() as d:
            data_dir = Path(d) / "teatree"
            data_dir.mkdir(parents=True)
            reactions_jsonl = data_dir / "slack-reactions.jsonl"
            reaction_event = {
                "type": "reaction_added",
                "user": _HIJACK_USER,
                "reaction": "thumbsup",
                "item": {"type": "message", "channel": _HIJACK_CHANNEL, "ts": _HIJACK_TS},
                "event_ts": _HIJACK_TS,
            }
            payload = json.dumps({"overlay": "hijacked", "event": reaction_event})
            reactions_jsonl.write_text(payload + "\n", encoding="utf-8")

            with (
                patch.dict("os.environ", {"CLAUDE_SESSION_ID": "intruder-session", "XDG_DATA_HOME": str(d)}),
                patch(
                    "teatree.core.backend_factory.iter_overlay_backends",
                    return_value=overlay_backends,
                ),
            ):
                call_command("loops_tick", stdout=StringIO())

            # The non-owner SKIP fired before run_tick: the reactions
            # queue was never drained (drain_reactions_queue renames +
            # unlinks the file, so its survival proves the scanner never
            # ran), and no ReviewAssignment row was created (no reviewer
            # dispatched).
            assert reactions_jsonl.is_file(), (
                "non-owner session drained slack-reactions.jsonl — #1078 owner gate reverted "
                "(the reaction-driven loop-hijack regressed)"
            )
            assert reactions_jsonl.read_text(encoding="utf-8") == payload + "\n"

        assert ReviewAssignment.objects.count() == 0, (
            "non-owner session created a ReviewAssignment (dispatched a reviewer) — #1078 owner gate reverted"
        )
        assert backend.react_calls == []
