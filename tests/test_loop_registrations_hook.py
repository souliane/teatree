# test-path: cross-cutting — tests hooks/scripts/loop_registrations.py (hooks/); no src/teatree/ mirror.
"""Owner session auto-registers the reactive infra loops at session start (PR-28).

PR-28 retired the native ``/loop`` cron mirror of the DB ``Loop`` rows — the
singleton ``t3 worker`` owns that cadence now. The owner session's
``UserPromptSubmit`` handler emits ONLY the three always-on reactive infra slots
(Slack-answer, self-improve, drain-queue), each via the ``/loop <duration>`` form
(sub-minute cadence, not a cron). A non-owner / fresh session emits nothing; a
loser with a live foreign owner backs off and writes no marker. The seam
``teatree.loop.loop_cadences.reactive_slot_directives`` is the single source of
truth, shared with the ``/t3:health`` skill and the ``t3 loop <slot> start`` CLI.

The pure prompt recognisers (``is_bare_loop_tick_prompt`` / ``loop_name_from_prompt``)
stay: ``hook_router`` and ``cron_tracking`` still classify a per-loop tick prompt
(fired by the worker's subprocess tick, or a stale pre-flip cron) without importing
teatree.
"""

import contextlib
import io
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import django.test
import pytest

import hooks.scripts.hook_router as router
from hooks.scripts import loop_registrations
from hooks.scripts.loop_registrations import emit_loop_registrations, is_bare_loop_tick_prompt, loop_name_from_prompt
from teatree.core.models import LoopLease


def _bare_tick_prompt(name: str) -> str:
    """The bare per-loop tick prompt the worker's subprocess tick fires (recogniser input)."""
    return f"Run `t3 loops tick --loop {name}` in Bash, then briefly report the tick summary."


_THREE_REACTIVE = ["/loop 5m /loop-slack-answer", "/loop 30m /loop-self-improve", "/loop 2m /loop-drain-queue"]


class TestEmitLoopRegistrations:
    def test_emits_the_reactive_slot_prose_when_slots_resolve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(loop_registrations, "_reactive_slot_directives", lambda: _THREE_REACTIVE)
        monkeypatch.setattr(loop_registrations, "_worker_owns_cadence", lambda: False)  # isolate reactive behaviour
        out = io.StringIO()
        emitted = emit_loop_registrations(out)
        text = out.getvalue()

        assert emitted is True
        # No structured ``register_cron`` directive is ever emitted now (worker owns the cadence).
        assert "hookSpecificOutput" not in text
        assert "register_cron" not in text
        assert "reactive infra loops" in text
        for directive in _THREE_REACTIVE:
            assert directive in text

    def test_fail_open_silent_when_no_reactive_slot_and_no_reminder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No reactive slot AND the worker not owning the cadence (no decommission
        # reminder) → the owner session stays silent.
        monkeypatch.setattr(loop_registrations, "_reactive_slot_directives", list)
        monkeypatch.setattr(loop_registrations, "_worker_owns_cadence", lambda: False)
        out = io.StringIO()
        assert emit_loop_registrations(out) is False
        assert out.getvalue() == ""


class TestCronDecommissionDirective:
    """The one-time CronDelete reminder for stale pre-flip native crons (PR-28)."""

    def test_directive_when_worker_owns_cadence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(loop_registrations, "_worker_owns_cadence", lambda: True)
        directive = loop_registrations.cron_decommission_directive()
        assert directive is not None
        assert "CronDelete" in directive
        assert "t3 loops tick --loop <name>" in directive

    def test_none_when_worker_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(loop_registrations, "_worker_owns_cadence", lambda: False)
        assert loop_registrations.cron_decommission_directive() is None


class TestPerLoopPromptRecognition:
    """The hot-path recogniser stays in lock-step with the worker's tick-prompt shape."""

    def test_recognises_the_bare_tick_prompt(self) -> None:
        prompt = _bare_tick_prompt("dream")
        assert is_bare_loop_tick_prompt(prompt) is True
        assert loop_name_from_prompt(prompt) == "dream"

    def test_a_prompt_with_user_content_is_not_a_bare_tick(self) -> None:
        prompt = _bare_tick_prompt("inbox") + " also please rebase my branch"
        assert is_bare_loop_tick_prompt(prompt) is False
        # The command is still extractable for cron-job naming.
        assert loop_name_from_prompt(prompt) == "inbox"

    def test_a_genuine_user_prompt_is_neither(self) -> None:
        assert is_bare_loop_tick_prompt("fix the failing test") is False
        assert loop_name_from_prompt("fix the failing test") is None


@pytest.fixture
def owner_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(router, "STATE_DIR", state)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("T3_AUTOLOAD", "1")
    session_id = "owner-session"
    (state / f"{session_id}.teatree-active").touch()  # opted into teatree
    monkeypatch.setattr(router, "_tick_meta_stale", lambda: True)
    monkeypatch.setattr(loop_registrations, "_reactive_slot_directives", lambda: _THREE_REACTIVE)
    return session_id


class TestOwnerSessionEmitsReactiveSlots:
    def test_owner_emits_the_reactive_slot_registrations(
        self, owner_session: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        router.handle_enforce_loop_on_prompt({"session_id": owner_session})
        out = capsys.readouterr().out
        assert "reactive infra loops" in out
        assert "hookSpecificOutput" not in out  # never a cron directive

    def test_owner_re_emit_is_suppressed_by_pending_marker(
        self, owner_session: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        router.handle_enforce_loop_on_prompt({"session_id": owner_session})
        capsys.readouterr()  # drain the first emission
        router.handle_enforce_loop_on_prompt({"session_id": owner_session})
        assert capsys.readouterr().out == ""  # emit-once per session

    def test_non_owner_session_emits_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(router, "STATE_DIR", state)
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        # No ``.teatree-active`` marker => not the loop owner => never registers.
        monkeypatch.setattr(loop_registrations, "_reactive_slot_directives", lambda: _THREE_REACTIVE)
        router.handle_enforce_loop_on_prompt({"session_id": "stranger"})
        assert capsys.readouterr().out == ""

    def test_opted_in_loser_with_live_foreign_owner_registers_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A second OPTED-IN session that finds a LIVE different-session owner registers NOTHING.

        The loser must back off AUTOMATICALLY: emit nothing AND write no pending marker.
        """
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(router, "STATE_DIR", state)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        loser = "loser-session"
        (state / f"{loser}.teatree-active").touch()  # the loser DID opt into teatree
        monkeypatch.setattr(loop_registrations, "_reactive_slot_directives", lambda: _THREE_REACTIVE)
        # A DIFFERENT, live (alive pid) session already holds the tick-owner record.
        router._write_loop_registry(
            {
                router._OWNER_LOOP: {
                    "session_id": "master-session",
                    "agent_id": "",
                    "pid": os.getpid(),
                    "heartbeat_ts": 0,
                }
            }
        )

        router.handle_enforce_loop_on_prompt({"session_id": loser})

        assert capsys.readouterr().out == "", "a loser with a LIVE foreign owner must register NOTHING"
        assert not (state / f"{loser}.loop-pending").is_file(), "the loser must write no pending marker (no nudge)"
        assert router._read_loop_registry()[router._OWNER_LOOP]["session_id"] == "master-session"


class TestTakeOverReconcilesFileRegistry(django.test.TestCase):
    """A DB ``--take-over`` reconciles a still-alive foreign file owner, then emits (#2851).

    ``t3 loop claim --take-over`` writes ONLY the DB ``LoopLease`` row, never the
    ``_OWNER_LOOP`` file registry. While the displaced owner stays alive in the
    file registry, the new owner's ``_claim_loop_ownership`` must consult the DB
    lease, see the hand-off, REWRITE the file registry to itself, and WIN — so
    ``_session_owns_loop`` reads True in the SAME hook and the emit path registers
    the reactive slots for the new owner.
    """

    def test_db_take_over_reconciles_stale_file_owner_and_registers(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        state = tmp / "state"
        state.mkdir(parents=True, exist_ok=True)
        new_owner = "new-owner-session"
        (state / f"{new_owner}.teatree-active").touch()  # the new owner opted in
        # An explicit ``t3 loop claim --take-over`` moved the LIVE DB lease to NEW.
        won, _ = LoopLease.objects.take_over_ownership("t3-master", session_id=new_owner, owner_pid=os.getpid())
        assert won

        buf = io.StringIO()
        with (
            mock.patch.object(router, "STATE_DIR", state),
            mock.patch.dict(os.environ, {"T3_LOOP_REGISTRY_DIR": str(tmp / "data"), "T3_AUTOLOAD": "1"}),
            mock.patch.object(loop_registrations, "_reactive_slot_directives", return_value=_THREE_REACTIVE),
            contextlib.redirect_stdout(buf),
        ):
            # The displaced owner is STILL ALIVE in the file registry.
            router._write_loop_registry(
                {
                    router._OWNER_LOOP: {
                        "session_id": "displaced-owner",
                        "agent_id": "",
                        "pid": os.getpid(),
                        "heartbeat_ts": 0,
                    }
                }
            )
            router.handle_enforce_loop_on_prompt({"session_id": new_owner})
            owns_loop = router._session_owns_loop(new_owner)
            reconciled_owner = router._read_loop_registry()[router._OWNER_LOOP]["session_id"]

        # The file registry is reconciled to NEW, so it owns the loop in this hook.
        assert owns_loop is True
        assert reconciled_owner == new_owner
        # And the emit path registered the reactive slots for NEW.
        assert "reactive infra loops" in buf.getvalue()


class TestReactiveSlotSeam:
    """The reactive ``/loop`` directives resolve end-to-end from the real seam (#2650).

    ``teatree.loop.loop_cadences.reactive_slot_directives`` is a pure ``os.environ``
    read (no DB), so the three always-on infra loops resolve even when the DB is
    unreachable.
    """

    def test_real_seam_yields_the_three_reactive_loop_directives(self) -> None:
        directives = loop_registrations._reactive_slot_directives()
        blob = "\n".join(directives)
        assert len(directives) == 3
        assert all(directive.startswith("/loop ") for directive in directives)
        assert "t3 loop slack-answer run" in blob
        assert "t3 loop self-improve run --tier cheap" in blob
        assert "t3 loop drain-queue run" in blob
