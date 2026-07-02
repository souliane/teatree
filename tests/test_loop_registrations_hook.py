# test-path: cross-cutting — tests hooks/scripts/loop_registrations.py (hooks/); no src/teatree/ mirror.
"""Owner session auto-registers the background loops at session start (#2650).

The owner session's ``UserPromptSubmit`` handler emits two families: ONE
``register_cron`` directive per ENABLED ``Loop`` row (DB loops) AND one
``/loop <duration>`` directive per always-on reactive infra slot (Slack-answer,
self-improve, drain-queue). A non-owner / fresh session emits nothing; an owner
with NO enabled DB loops still auto-registers the three reactive slots — they have
no DB row and no master tick to piggyback on, so the owner drives them directly.
The seams ``teatree.loops.claude_specs`` (DB loops) and
``teatree.loop.loop_cadences`` (reactive slots) are the single sources of truth,
shared with the ``/t3:loops`` skill and the ``t3 loop <slot> start`` CLI, so the
hook directives and the CLI affordances agree.
"""

import contextlib
import io
import json
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
from teatree.core.models import Loop, LoopLease, Prompt
from teatree.loops.claude_specs import ClaudeLoopSpec, loop_run_prompt


def _two_specs() -> list[ClaudeLoopSpec]:
    return [
        ClaudeLoopSpec("t3-loop-inbox", "*/1 * * * *", loop_run_prompt("inbox")),
        ClaudeLoopSpec("t3-loop-ship", "*/5 * * * *", loop_run_prompt("ship")),
    ]


class TestEmitLoopRegistrations:
    def test_emits_one_register_cron_entry_per_enabled_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(loop_registrations, "_enabled_loop_specs", _two_specs)
        out = io.StringIO()
        emitted = emit_loop_registrations(out)
        text = out.getvalue()

        assert emitted is True
        directive = json.loads(text.splitlines()[0])
        loops = directive["hookSpecificOutput"]["loops"]
        assert directive["hookSpecificOutput"]["action"] == "register_cron"
        assert [entry["slot_id"] for entry in loops] == ["t3-loop-inbox", "t3-loop-ship"]
        assert loops[0]["cron"] == "*/1 * * * *"
        assert "t3 loops tick --loop inbox" in loops[0]["prompt"]
        # The prose fallback lists each loop's CronCreate so a harness that does
        # not read the structured directive still registers all of them.
        assert "t3-loop-inbox" in text
        assert "t3-loop-ship" in text
        # Verify-by-reread (#1192): the bulk SessionStart nudge must instruct the
        # same confirm step the manual /t3:loops skill path already has, per
        # loop name (not slot_id) — the CLI's positional arg is the DB Loop name.
        assert "verify-cron" in text
        assert "CronList" in text
        assert "t3 loop verify-cron inbox --cron-list-json" in text
        assert "t3 loop verify-cron ship --cron-list-json" in text

    def test_no_db_loops_still_auto_registers_the_three_reactive_slots(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No enabled DB loop, but the three always-on reactive slots have no DB
        # row of their own — the owner registers each as its own ``/loop`` (#2650),
        # so a fresh owner session with an empty Loop table still drives them.
        monkeypatch.setattr(loop_registrations, "_enabled_loop_specs", list)
        out = io.StringIO()
        emitted = emit_loop_registrations(out)
        text = out.getvalue()

        assert emitted is True
        # No DB loops => no structured ``register_cron`` directive, only the
        # reactive ``/loop`` prose.
        assert "hookSpecificOutput" not in text
        assert "t3 loop slack-answer run" in text
        assert "t3 loop self-improve run --tier cheap" in text
        assert "t3 loop drain-queue run" in text

    def test_fail_open_silent_when_both_seams_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Each seam accessor swallows errors and returns []; with NEITHER a DB
        # loop NOR a resolvable reactive slot, the public entry point stays silent
        # — never an exception into the fast hook.
        monkeypatch.setattr(loop_registrations, "_enabled_loop_specs", list)
        monkeypatch.setattr(loop_registrations, "_reactive_slot_directives", list)
        out = io.StringIO()
        assert emit_loop_registrations(out) is False
        assert out.getvalue() == ""


class TestPerLoopPromptRecognition:
    """The hot-path recogniser stays in lock-step with the seam's generated prompt."""

    def test_recognises_the_seams_generated_prompt(self) -> None:
        prompt = loop_run_prompt("dream")
        assert is_bare_loop_tick_prompt(prompt) is True
        assert loop_name_from_prompt(prompt) == "dream"

    def test_a_prompt_with_user_content_is_not_a_bare_tick(self) -> None:
        prompt = loop_run_prompt("inbox") + " also please rebase my branch"
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
    monkeypatch.setattr(router, "_session_has_loop", lambda sid: False)
    return session_id


class TestOwnerSessionEmitsPerLoop:
    def test_owner_emits_one_directive_per_enabled_loop(
        self, owner_session: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(loop_registrations, "_enabled_loop_specs", _two_specs)
        router.handle_enforce_loop_on_prompt({"session_id": owner_session})
        out = capsys.readouterr().out
        directive = json.loads(out.splitlines()[0])
        loops = directive["hookSpecificOutput"]["loops"]
        assert [entry["slot_id"] for entry in loops] == ["t3-loop-inbox", "t3-loop-ship"]

    def test_owner_with_no_db_loops_still_auto_registers_the_three_reactive_slots(
        self, owner_session: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A FRESH owner with an empty Loop table auto-registers all three reactive slots (#2650).

        This is the merge gate for master-tick removal: with no master tick to
        piggyback the reactive cycles onto, the owner bootstrap must drive
        Slack-answer / self-improve / drain-queue itself, each as its own
        ``/loop``.  Runs the REAL ``teatree.loop.loop_cadences`` seam end-to-end.
        """
        monkeypatch.setattr(loop_registrations, "_enabled_loop_specs", list)
        router.handle_enforce_loop_on_prompt({"session_id": owner_session})
        out = capsys.readouterr().out

        assert "t3 loop slack-answer run" in out
        assert "t3 loop self-improve run --tier cheap" in out
        assert "t3 loop drain-queue run" in out
        # Sub-minute cadence => the ``/loop <duration>`` form, never a cron directive.
        assert "/loop " in out
        assert "hookSpecificOutput" not in out

    def test_owner_emits_when_tick_meta_fresh_but_session_has_no_cron(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Regression for #2714: release+claim must re-register even when tick-meta is fresh.

        tick-meta.json can be fresh because the previous owner session was ticking
        normally before ``t3 loop release``.  A new session that claims ownership
        afterwards has no registered cron yet — _tick_meta_stale() returning False
        must NOT prevent registration.  Only _session_has_loop() is the correct gate.
        """
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(router, "STATE_DIR", state)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        session_id = "new-owner-after-claim"
        (state / f"{session_id}.teatree-active").touch()
        # tick-meta is FRESH — previous owner was ticking before release.
        monkeypatch.setattr(router, "_tick_meta_stale", lambda: False)
        # This new session has no registered cron yet.
        monkeypatch.setattr(router, "_session_has_loop", lambda sid: False)
        monkeypatch.setattr(loop_registrations, "_enabled_loop_specs", _two_specs)

        router.handle_enforce_loop_on_prompt({"session_id": session_id})

        out = capsys.readouterr().out
        assert out != "", "must emit registration even when tick-meta is fresh after claim"
        directive = json.loads(out.splitlines()[0])
        loops = directive["hookSpecificOutput"]["loops"]
        assert len(loops) == 2

    def test_non_owner_session_emits_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(router, "STATE_DIR", state)
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        # No ``.teatree-active`` marker => not the loop owner => never registers.
        monkeypatch.setattr(loop_registrations, "_enabled_loop_specs", _two_specs)
        router.handle_enforce_loop_on_prompt({"session_id": "stranger"})
        assert capsys.readouterr().out == ""

    def test_opted_in_loser_with_live_foreign_owner_registers_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A second OPTED-IN session that finds a LIVE different-session owner registers NOTHING (#2650).

        The contention bug: BOTH an opted-in owner session AND an opted-in second
        session emitted ``register_cron`` directives, so both registered native
        ``/loop`` crons whose ``t3 loops tick --loop <name>`` runs then ping-ponged
        the per-loop ``loop:<name>`` leases — ~half the loops SKIP every round. The
        loser must back off AUTOMATICALLY: emit nothing AND write no pending marker,
        so the PreToolUse nudge (which requires the marker) also stays silent. Only
        the rightful owner's crons fire. Distinct from
        :meth:`test_non_owner_session_emits_nothing`, which covers a session that
        never opted into teatree at all (a colleague who merely cloned the repo).
        """
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(router, "STATE_DIR", state)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        loser = "loser-session"
        (state / f"{loser}.teatree-active").touch()  # the loser DID opt into teatree
        monkeypatch.setattr(router, "_session_has_loop", lambda sid: False)
        monkeypatch.setattr(loop_registrations, "_enabled_loop_specs", _two_specs)
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
        # The loser must NOT have wrested the tick-owner record from the live master.
        assert router._read_loop_registry()[router._OWNER_LOOP]["session_id"] == "master-session"


class TestTakeOverReconcilesFileRegistry(django.test.TestCase):
    """A DB ``--take-over`` reconciles a still-alive foreign file owner, then emits (#2851).

    ``t3 loop claim --take-over`` writes ONLY the DB ``LoopLease`` row, never the
    ``_OWNER_LOOP`` file registry. While the displaced owner stays alive in the
    file registry, the new owner's ``_claim_loop_ownership`` must consult the DB
    lease, see the hand-off, REWRITE the file registry to itself, and WIN — so
    ``_session_owns_loop`` reads True in the SAME hook and the emit path registers
    crons for the new owner. The flip side of
    :meth:`TestOwnerSessionEmitsPerLoop.test_opted_in_loser_with_live_foreign_owner_registers_nothing`:
    without the DB consult the new owner emits nothing and the loop stalls until
    the displaced session ends (the HOLD finding on #2851). A ``django.test.TestCase``
    because the take-over is recorded in the real DB ``LoopLease`` row the hook reads.
    """

    def test_db_take_over_reconciles_stale_file_owner_and_registers(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        state = tmp / "state"
        state.mkdir(parents=True, exist_ok=True)
        new_owner = "new-owner-session"
        (state / f"{new_owner}.teatree-active").touch()  # the new owner opted in
        # An explicit ``t3 loop claim --take-over`` moved the LIVE DB lease to NEW.
        won, _ = LoopLease.objects.claim_ownership(
            "t3-master", session_id=new_owner, take_over=True, owner_pid=os.getpid()
        )
        assert won

        buf = io.StringIO()
        with (
            mock.patch.object(router, "STATE_DIR", state),
            mock.patch.dict(os.environ, {"T3_LOOP_REGISTRY_DIR": str(tmp / "data"), "T3_AUTOLOAD": "1"}),
            mock.patch.object(router, "_session_has_loop", return_value=False),
            mock.patch.object(loop_registrations, "_enabled_loop_specs", _two_specs),
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
        # And the emit path registered one cron per enabled loop for NEW.
        out = buf.getvalue()
        assert out != "", "the reconciled new owner must register its crons"
        loops = json.loads(out.splitlines()[0])["hookSpecificOutput"]["loops"]
        assert len(loops) == 2


def _prompt(name: str = "demo-prompt") -> Prompt:
    prompt, _ = Prompt.objects.get_or_create(name=name, defaults={"body": "do x"})
    return prompt


class TestSeamDrivesDirectivesFromTheDb(django.test.TestCase):
    """End-to-end against the real seam: the directives mirror the enabled rows."""

    def test_directives_reflect_enabled_rows(self) -> None:
        Loop.objects.create(name="hook-on", delay_seconds=300, prompt=_prompt(), enabled=True)
        Loop.objects.create(name="hook-off", delay_seconds=60, prompt=_prompt(), enabled=False)
        directives = loop_registrations.loop_registration_directives()
        slot_ids = {entry["slot_id"] for entry in directives}
        assert "t3-loop-hook-on" in slot_ids
        assert "t3-loop-hook-off" not in slot_ids


class TestReactiveSlotSeam:
    """The reactive ``/loop`` directives resolve end-to-end from the real seam (#2650).

    ``teatree.loop.loop_cadences.reactive_slot_directives`` is a pure ``os.environ``
    read (no DB), so the three always-on infra loops resolve even when the DB is
    unreachable — only the DB-loop directives would fall away.
    """

    def test_real_seam_yields_the_three_reactive_loop_directives(self) -> None:
        directives = loop_registrations._reactive_slot_directives()
        blob = "\n".join(directives)
        assert len(directives) == 3
        assert all(directive.startswith("/loop ") for directive in directives)
        assert "t3 loop slack-answer run" in blob
        assert "t3 loop self-improve run --tier cheap" in blob
        assert "t3 loop drain-queue run" in blob
