"""Integration tests for the tick piggyback safety net (#1107 Prong B).

Defense-in-depth for the #1107 incident: even when loop-owner can never be
claimed (the pure-cron / no-session state Prong A cannot rescue), a won
``t3 loop tick`` must still drive the reactive Slack-answer cycle and the
self-improve monitor so user DMs get :eyes:/answered and smells get
recorded. The cycles run behind their existing dedicated ``LoopLease``
CAS so a real dedicated slot is never double-run (#1014/#1075 preserved).

Test-Writing Doctrine: real DB rows + ``call_command("loop_tick")``; only
the Slack network (a recording fake) and the RAM probe are faked. The
cycle, ``run_tick``, and the lease CAS are all real.
"""

import io
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from teatree.core.models import LoopLease, PendingChatInjection, SelfImproveFiring, Ticket
from teatree.core.models.pull_request import PullRequest
from teatree.types import RawAPIDict

pytestmark = pytest.mark.django_db


@dataclass
class RecordingBackend:
    """Slack fake — clone of ``test_loop_slack_answer_command.RecordingBackend``."""

    reactions: list[tuple[str, str, str]] = field(default_factory=list)
    replies: list[tuple[str, str, str]] = field(default_factory=list)

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        self.replies.append((channel, ts, text))
        return {"ok": True}

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return "D1"

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = (channel, ts)
        return "https://slack/p1"

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.reactions.append((channel, ts, emoji))
        return {"ok": True}

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""


def _no_session_env() -> dict[str, str]:
    """The pure-cron incident environment — no session id, no registry dir."""
    import os  # noqa: PLC0415

    return {
        k: v
        for k, v in os.environ.items()
        if k not in {"CLAUDE_SESSION_ID", "T3_LOOP_SESSION_ID", "T3_LOOP_REGISTRY_DIR"}
    }


def _patch_resolver(backend: RecordingBackend):
    return patch(
        "teatree.core.backend_factory.messaging_from_overlay",
        return_value=backend,
    )


class TestTickPiggybackSlackAnswer:
    def test_tick_reacts_eyes_on_unreplied_dm_without_fat_loop_owner(self) -> None:
        """RED on main: ``loop_tick`` never invokes the reactive cycle.

        Pure-cron incident state — no session env vars AND no loop-registry
        — so Prong A cannot resolve an owner; this proves B is an
        independent safety net (the auto-claim-for-free CAS still wins the
        unowned ``loop-owner`` for an anonymous caller).
        """
        row = PendingChatInjection.record(channel="C9", slack_ts="9.0", text="thanks!")
        assert row is not None
        backend = RecordingBackend()
        out = io.StringIO()
        with (
            patch.dict("os.environ", _no_session_env(), clear=True),
            patch("teatree.core.connector_preflight.run_connector_preflight"),
            patch("teatree.core.management.commands.loop_tick._registry_jobs_builder", return_value=[]),
            _patch_resolver(backend),
        ):
            call_command("loop_tick", stdout=out)

        row.refresh_from_db()
        assert row.eyes_reacted_at is not None
        assert ("C9", "9.0", "eyes") in backend.reactions

    def test_tick_piggyback_skips_when_dedicated_slack_answer_lease_held(self) -> None:
        """A real dedicated slot holding the lease wins; piggyback yields."""
        row = PendingChatInjection.record(channel="C9", slack_ts="9.0", text="thanks!")
        assert row is not None
        LoopLease.objects.acquire("loop-slack-answer", owner="pid-realslot", lease_seconds=600)
        backend = RecordingBackend()
        out = io.StringIO()
        with (
            patch.dict("os.environ", _no_session_env(), clear=True),
            patch("teatree.core.connector_preflight.run_connector_preflight"),
            patch("teatree.core.management.commands.loop_tick._registry_jobs_builder", return_value=[]),
            _patch_resolver(backend),
        ):
            call_command("loop_tick", stdout=out)

        row.refresh_from_db()
        assert row.eyes_reacted_at is None
        assert backend.reactions == []
        # Quiet-exit contract (#2417): a non-actionable tick emits no WARN line.
        assert "WARN" not in out.getvalue()

    def test_tick_piggyback_throttled_within_cadence(self) -> None:
        """Two ticks inside the cadence window → exactly one eyes reaction."""
        row = PendingChatInjection.record(channel="C9", slack_ts="9.0", text="thanks!")
        assert row is not None
        backend = RecordingBackend()
        with (
            patch.dict(
                "os.environ",
                {**_no_session_env(), "T3_SLACK_ANSWER_CADENCE": "3600"},
                clear=True,
            ),
            patch("teatree.core.connector_preflight.run_connector_preflight"),
            patch("teatree.core.management.commands.loop_tick._registry_jobs_builder", return_value=[]),
            _patch_resolver(backend),
        ):
            call_command("loop_tick", stdout=io.StringIO())
            call_command("loop_tick", stdout=io.StringIO())

        assert backend.reactions.count(("C9", "9.0", "eyes")) == 1


class TestTickPiggybackSelfImprove:
    def test_tick_runs_self_improve_cycle_without_fat_loop_owner(self, tmp_path: Path) -> None:
        """RED on main: a Phase-1 detector condition records no firing.

        A merged ``PullRequest`` whose URL is on the rendered statusline is
        the ``StaleStatuslineEntryDetector`` smell; the piggybacked
        ``run_tier(cheap)`` records a ``SelfImproveFiring`` row. Real file
        IO — a real ``statusline.txt`` under a tmp ``XDG_DATA_HOME`` (the
        path ``_default_statusline_reader`` reads) — only the RAM probe is
        faked so the budget gate is deterministic.
        """
        url = "https://github.com/o/r/pull/4242"
        ticket = Ticket.objects.create(overlay="acme", issue_url=url + "/issues")
        pr = PullRequest.objects.create(ticket=ticket, overlay="acme", url=url, repo="o/r", iid="4242")
        pr.mark_merged()
        pr.save()

        teatree_dir = tmp_path / "teatree"
        teatree_dir.mkdir()
        # The detector reads the statusline via the XDG default path. The
        # tick's own render is redirected to a separate file
        # (``--statusline-file``) so it does not clobber this seeded input
        # before the piggybacked self-improve cycle reads it.
        (teatree_dir / "statusline.txt").write_text(f"in flight: {url}", encoding="utf-8")
        tick_statusline = tmp_path / "tick-statusline.txt"

        out = io.StringIO()
        with (
            patch.dict("os.environ", {**_no_session_env(), "XDG_DATA_HOME": str(tmp_path)}, clear=True),
            patch("teatree.core.connector_preflight.run_connector_preflight"),
            patch("teatree.core.management.commands.loop_tick._registry_jobs_builder", return_value=[]),
            patch("teatree.loop.self_improve.budget._read_ram_used_percent", return_value=0.0),
        ):
            call_command("loop_tick", "--statusline-file", str(tick_statusline), stdout=out)

        assert SelfImproveFiring.objects.filter(detector="stale_statusline_entry").exists()


class TestTickPiggybackOwnerGate:
    def test_non_owner_skip_does_not_piggyback(self) -> None:
        """Anti-#1073: a foreign-session SKIP must NOT piggyback.

        GREEN on main by accident (the #1073 owner gate returns before any
        piggyback could exist) — this is the anti-hijack guard that must
        stay GREEN after the fix, not a vacuous test. Mirrors
        ``test_loop_tick_command.TestLoopOwnerGate``.
        """
        LoopLease.objects.claim_ownership("loop-owner", session_id="owner-session")
        row = PendingChatInjection.record(channel="C9", slack_ts="9.0", text="thanks!")
        assert row is not None
        backend = RecordingBackend()
        out = io.StringIO()
        with (
            patch.dict("os.environ", {**_no_session_env(), "CLAUDE_SESSION_ID": "intruder"}, clear=True),
            patch("teatree.core.connector_preflight.run_connector_preflight"),
            _patch_resolver(backend),
        ):
            call_command("loop_tick", stdout=out)

        row.refresh_from_db()
        assert "SKIP" in out.getvalue()
        assert "loop not owned by this session" in out.getvalue()
        assert row.eyes_reacted_at is None
        assert backend.reactions == []


class TestPiggybackCadenceParsing:
    """The defensive ``int()`` fallbacks (mirror ``loop_tick._loop_owner_ttl_seconds``)."""

    def test_slack_answer_cadence_falls_back_on_non_integer(self) -> None:
        from teatree.loop.tick_piggyback import _slack_answer_cadence_seconds  # noqa: PLC0415

        with patch.dict("os.environ", {"T3_SLACK_ANSWER_CADENCE": "not-an-int"}):
            assert _slack_answer_cadence_seconds() == 20

    def test_self_improve_cadence_falls_back_on_non_integer(self) -> None:
        from teatree.loop.tick_piggyback import _self_improve_cadence_seconds  # noqa: PLC0415

        with patch.dict("os.environ", {"T3_SELF_IMPROVE_CHEAP_CADENCE": "garbage"}):
            assert _self_improve_cadence_seconds() == 1800


class TestPiggybackCrashIsolation:
    """A failing cycle must never propagate out of the safety net."""

    def test_one_cycle_raising_is_logged_and_the_other_still_runs(self) -> None:
        from teatree.loop import tick_piggyback  # noqa: PLC0415

        calls: list[str] = []
        msg = "slack cycle exploded"

        def _boom() -> None:
            raise RuntimeError(msg)

        def _ok() -> None:
            calls.append("self-improve")

        with (
            patch.object(tick_piggyback, "_piggyback_slack_answer", _boom),
            patch.object(tick_piggyback, "_piggyback_self_improve", _ok),
            patch("teatree.loop.queue_drain._piggyback_drain_queue", lambda: None),
            patch.object(tick_piggyback.logger, "warning") as warn,
        ):
            tick_piggyback.run_piggyback_cycles()

        assert calls == ["self-improve"]
        warn.assert_called_once()
        assert "Slack-answer cycle failed" in warn.call_args.args[0]

    def test_self_improve_cycle_raising_is_logged(self) -> None:
        from teatree.loop import tick_piggyback  # noqa: PLC0415

        msg = "self-improve exploded"

        def _boom() -> None:
            raise RuntimeError(msg)

        with (
            patch.object(tick_piggyback, "_piggyback_slack_answer", lambda: None),
            patch.object(tick_piggyback, "_piggyback_self_improve", _boom),
            patch("teatree.loop.queue_drain._piggyback_drain_queue", lambda: None),
            patch.object(tick_piggyback.logger, "warning") as warn,
        ):
            tick_piggyback.run_piggyback_cycles()

        warn.assert_called_once()
        assert "self-improve cycle failed" in warn.call_args.args[0]

    def test_queue_drain_cycle_raising_is_logged(self) -> None:
        from teatree.loop import tick_piggyback  # noqa: PLC0415

        msg = "drain exploded"

        def _boom() -> None:
            raise RuntimeError(msg)

        with (
            patch.object(tick_piggyback, "_piggyback_slack_answer", lambda: None),
            patch.object(tick_piggyback, "_piggyback_self_improve", lambda: None),
            patch("teatree.loop.queue_drain._piggyback_drain_queue", _boom),
            patch.object(tick_piggyback.logger, "warning") as warn,
        ):
            tick_piggyback.run_piggyback_cycles()

        warn.assert_called_once()
        assert "queue-drain cycle failed" in warn.call_args.args[0]
