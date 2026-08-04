"""Tests for ``t3 <overlay> handover`` and ``t3 loop whoami`` commands."""

import json
import os
import pathlib
import tempfile
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.fast_push import FastPushOutcome, LeakFinding
from teatree.core.handover import claim_handovers
from teatree.core.handover_orchestration import SubagentPush
from teatree.core.handover_wrapup import SUBAGENT_MARKER_START
from teatree.core.management.commands.handover import Command
from teatree.core.models import LoopLease, SessionHandover, Ticket


def _call(*args: str, **kwargs) -> str:
    buf = StringIO()
    call_command(*args, stdout=buf, stderr=StringIO(), **kwargs)
    return buf.getvalue()


def _call_human(*args: str, **kwargs) -> str:
    """Capture the human view — the emit() seam routes it to stderr, stdout stays JSON-only."""
    err = StringIO()
    call_command(*args, stdout=StringIO(), stderr=err, **kwargs)
    return err.getvalue()


class _PinnedSessionTestCase(TestCase):
    """Pin this 'session' id and isolate the snapshot dir + XDG mirror.

    A PreCompact snapshot is seeded so ``create`` has durable state to hand over;
    a hand-off with an empty payload now exits non-zero (#3551) and is asserted
    on its own by ``TestEmptyHandoverIsRefused``.

    Also stubs the directive-#8 sub-agent driver to a no-op so a hand-off in
    the test process never fast-pushes the real repo's worktrees; the coupling
    itself is asserted by ``TestHandoverDrivesSubagents`` with its own spy.
    """

    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tmp_path = pathlib.Path(tmp_dir.name)

        self._patch_env(
            T3_LOOP_SESSION_ID="this-session",
            TEATREE_CLAUDE_STATUSLINE_STATE_DIR=str(self.tmp_path / "state"),
            XDG_DATA_HOME=str(self.tmp_path / "xdg"),
        )
        self._unset_env("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "T3_DATA_DIR")
        (self.tmp_path / "state").mkdir(parents=True, exist_ok=True)
        (self.tmp_path / "state" / "t3-snapshot-this-session-precompact.md").write_text(
            "DURABLE STATE", encoding="utf-8"
        )
        self._patch("teatree.core.management.commands.handover.drive_subagents_to_fast_push", lambda *a, **k: [])

    def _patch(self, target: str, replacement: object) -> None:
        """Replace *target* for the duration of the test."""
        patched = mock.patch(target, replacement)
        patched.start()
        self.addCleanup(patched.stop)

    def _patch_env(self, **values: str) -> None:
        """Set env *values*, restoring the whole environment afterwards."""
        patched = mock.patch.dict(os.environ, values)
        patched.start()
        self.addCleanup(patched.stop)

    def _unset_env(self, *keys: str) -> None:
        """Drop *keys* from the (already-restorable) environment."""
        for key in keys:
            os.environ.pop(key, None)


class TestHandoverCreate(_PinnedSessionTestCase):
    def test_no_target_parks_for_next_when_no_owner(self) -> None:
        out = _call("handover", "create", json_output=True)
        data = json.loads(out)
        assert data["ok"] is True
        assert data["parked_for_next"] is True
        row = SessionHandover.objects.get()
        assert row.from_session == "this-session"
        assert row.to_session == ""

    def test_no_target_hands_to_live_loop_owner(self) -> None:
        LoopLease.objects.claim_ownership("t3-master", session_id="owner-X", owner_pid=os.getpid())
        out = _call("handover", "create", json_output=True)
        data = json.loads(out)
        assert data["to_session"] == "owner-X"
        assert data["parked_for_next"] is False

    def test_explicit_target(self) -> None:
        out = _call("handover", "create", to="target-Z", json_output=True)
        assert json.loads(out)["to_session"] == "target-Z"

    def test_human_output_reports_ok_and_the_mirror(self) -> None:
        # The non-JSON path: durable state present → "OK" status and the mirror path,
        # now routed to stderr so stdout stays a pure JSON channel (emit seam).
        out = _call_human("handover", "create", to="target-Z")
        assert "OK" in out
        assert "handed off to target-Z" in out
        assert "mirror written to" in out

    def test_create_writes_xdg_mirror(self) -> None:
        data = json.loads(_call("handover", "create", json_output=True))
        mirror = data["mirror_path"]
        assert "handover" in mirror
        assert pathlib.Path(mirror).is_file()

    def test_no_session_id_errors(self) -> None:
        self._unset_env("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "T3_LOOP_SESSION_ID")
        self._patch_env(T3_LOOP_REGISTRY_DIR="/nonexistent-registry-dir")
        with pytest.raises(SystemExit):
            _call("handover", "create", json_output=True)

    def test_claude_code_session_id_is_accepted(self) -> None:
        """The #3554 bug: a live Claude Code session exports only ``CLAUDE_CODE_SESSION_ID``."""
        self._unset_env("CLAUDE_SESSION_ID", "T3_LOOP_SESSION_ID")
        self._patch_env(T3_LOOP_REGISTRY_DIR="/nonexistent-registry-dir", CLAUDE_CODE_SESSION_ID="cc-session")
        (self.tmp_path / "state" / "t3-snapshot-cc-session-precompact.md").write_text("DURABLE", encoding="utf-8")
        data = json.loads(_call("handover", "create", json_output=True))
        assert data["ok"] is True
        assert SessionHandover.objects.get().from_session == "cc-session"


class TestHandoverDrivesSubagents(_PinnedSessionTestCase):
    """Directive #8 — ``handover create`` drives in-flight sub-agents through fast-push."""

    def test_create_invokes_the_subagent_driver_and_surfaces_pushes(self) -> None:
        calls: list[tuple] = []

        def _spy(repo: str, **kwargs) -> list[SubagentPush]:
            calls.append((repo, kwargs))
            outcome = FastPushOutcome(ok=True, branch="feat/x", committed=True, pushed=True, pr_url="http://pr/1")
            return [SubagentPush(worktree=pathlib.Path("/wt/agent-x"), branch="feat/x", driven=True, outcome=outcome)]

        self._patch("teatree.core.management.commands.handover.drive_subagents_to_fast_push", _spy)

        data = json.loads(_call("handover", "create", json_output=True))

        assert calls, "handover create must drive sub-agents to fast-push (directive #8)"
        pushes = data["subagent_pushes"]
        assert pushes[0]["branch"] == "feat/x"
        assert pushes[0]["pushed"] is True
        assert pushes[0]["pr_url"] == "http://pr/1"

    def test_no_drive_subagents_flag_skips_the_driver(self) -> None:
        calls: list = []
        self._patch(
            "teatree.core.management.commands.handover.drive_subagents_to_fast_push",
            lambda *a, **k: calls.append((a, k)) or [],
        )
        data = json.loads(_call("handover", "create", drive_subagents=False, json_output=True))
        assert calls == []
        assert data["subagent_pushes"] == []

    def test_driver_failure_never_fails_the_handover(self) -> None:
        def _boom(*_a, **_k) -> list:
            msg = "git exploded"
            raise RuntimeError(msg)

        self._patch("teatree.core.management.commands.handover.drive_subagents_to_fast_push", _boom)
        data = json.loads(_call("handover", "create", json_output=True))
        assert data["ok"] is True
        assert data["subagent_pushes"] == []
        assert SessionHandover.objects.count() == 1


class TestTheBarriersReturnsLandInThePayload(_PinnedSessionTestCase):
    """The receiver reads the ROW, so per-agent done/remaining must be IN it, not only printed (#4194)."""

    def test_each_agents_done_and_remaining_are_persisted(self) -> None:
        pushed = SubagentPush(
            worktree=pathlib.Path("/wt/agent-x"),
            branch="feat/x",
            driven=True,
            outcome=FastPushOutcome(ok=True, branch="feat/x", committed=True, pushed=True, pr_url="http://pr/1"),
        )
        refused = SubagentPush(
            worktree=pathlib.Path("/wt/agent-y"),
            branch="feat/y",
            driven=True,
            outcome=FastPushOutcome(
                ok=False, branch="feat/y", findings=[LeakFinding(gate="secrets", path="a.py", detail="token in a.py")]
            ),
        )
        self._patch(
            "teatree.core.management.commands.handover.drive_subagents_to_fast_push", lambda *a, **k: [pushed, refused]
        )

        data = json.loads(_call("handover", "create", json_output=True))

        payload = SessionHandover.objects.get().payload
        assert "Sub-agent wrap-up" in payload
        assert "feat/x" in payload
        assert "http://pr/1" in payload
        assert "feat/y" in payload
        assert "token in a.py" in payload, "what REMAINS is the half a receiver has to act on"
        assert data["subagent_count"] == 2

    def test_zero_agents_renders_an_explicit_line(self) -> None:
        """An absent section reads exactly like a barrier that never ran — the reported symptom."""
        _call("handover", "create", json_output=True)
        payload = SessionHandover.objects.get().payload
        assert "No in-flight sub-agent worktrees" in payload

    def test_skipping_the_barrier_writes_no_section(self) -> None:
        _call("handover", "create", drive_subagents=False, json_output=True)
        assert "Sub-agent wrap-up" not in SessionHandover.objects.get().payload


def _push(worktree: str, branch: str, *, error: str = "", pushed: bool = False) -> SubagentPush:
    if pushed:
        outcome = FastPushOutcome(ok=True, branch=branch, committed=True, pushed=True)
        return SubagentPush(worktree=pathlib.Path(worktree), branch=branch, driven=True, outcome=outcome)
    return SubagentPush(worktree=pathlib.Path(worktree), branch=branch, driven=False, error=error)


class TestOneWrapUpSectionPerRow(_PinnedSessionTestCase):
    """N hand-offs from one session leave ONE wrap-up block, carrying every agent seen (#4194).

    Ten hand-offs used to leave the receiver ten ``## Sub-agent wrap-up`` sections,
    each a snapshot of a different moment — the N-partially-contradictory-narratives
    problem this PR's single row was built to end, reproduced inside that row.
    """

    def _barrier(self, *rounds: list[SubagentPush]) -> None:
        """Return a different push set on each successive ``create``."""
        remaining = list(rounds)
        self._patch(
            "teatree.core.management.commands.handover.drive_subagents_to_fast_push",
            lambda *a, **k: remaining.pop(0) if remaining else [],
        )

    def test_a_second_hand_off_updates_one_wrap_up_section(self) -> None:
        self._barrier([_push("/wt/a", "feat/a", error="boom")], [_push("/wt/a", "feat/a", error="boom")])

        _call("handover", "create", body="FIRST", json_output=True)
        _call("handover", "create", body="SECOND", json_output=True)

        payload = SessionHandover.objects.get().payload
        assert payload.count(SUBAGENT_MARKER_START) == 1

    def test_an_agent_dropped_from_the_latest_barrier_is_still_named(self) -> None:
        """The discriminating test: under a REPLACE strategy A's record vanishes entirely."""
        self._barrier(
            [_push("/wt/a", "feat/a", error="refused: unpushed work"), _push("/wt/b", "feat/b", error="boom")],
            [_push("/wt/b", "feat/b", pushed=True)],
        )

        _call("handover", "create", body="FIRST", json_output=True)
        _call("handover", "create", body="SECOND", json_output=True)

        payload = SessionHandover.objects.get().payload
        assert payload.count(SUBAGENT_MARKER_START) == 1
        assert "/wt/a" in payload, "a worktree refused at hand-off #1 and since pruned must still be named"
        assert "refused: unpushed work" in payload
        assert "NOT enumerated at the latest barrier" in payload
        assert "/wt/b" in payload
        assert "committed, pushed" in payload, "the surviving agent carries its LATEST status"

    def test_a_changed_agent_status_is_updated_not_duplicated(self) -> None:
        self._barrier([_push("/wt/a", "feat/a", error="refused: leak")], [_push("/wt/a", "feat/a", pushed=True)])

        _call("handover", "create", body="FIRST", json_output=True)
        _call("handover", "create", body="SECOND", json_output=True)

        payload = SessionHandover.objects.get().payload
        assert payload.count("/wt/a") == 1
        assert "refused: leak" not in payload, "the stale status is UPDATED, not left beside the new one"
        assert "committed, pushed" in payload

    def test_the_completeness_assertion_refuses_a_duplicated_wrap_up_block(self) -> None:
        created = _call("handover", "create", body="BODY", json_output=True)
        row = SessionHandover.objects.get()
        assert json.loads(created)["completeness_ok"] is True
        SessionHandover.objects.filter(pk=row.pk).update(payload=f"{row.payload}\n\n{row.payload}")

        failures = Command._completeness_failures(pk=row.pk, expected="BODY", drove_subagents=True)

        assert any("sub-agent wrap-up sections" in failure for failure in failures)


class TestHandoverReportsTheIntegerRowId(_PinnedSessionTestCase):
    def test_json_carries_the_integer_primary_key(self) -> None:
        data = json.loads(_call("handover", "create", to="target-Z", json_output=True))
        row = SessionHandover.objects.get()
        assert data["handover_id"] == row.pk
        assert isinstance(data["handover_id"], int), "the issue asks for the row id, not a uuid"

    def test_the_human_line_leads_with_the_row_id(self) -> None:
        out = _call_human("handover", "create", to="target-Z")
        assert f"hand-off #{SessionHandover.objects.get().pk}" in out


class TestASecondCreateReportsWhatItAbsorbed(_PinnedSessionTestCase):
    def test_updated_existing_and_the_prior_byte_count_are_reported(self) -> None:
        first = json.loads(_call("handover", "create", body="FIRST STATE", to="target-Z", json_output=True))
        assert first["updated_existing"] is False
        assert first["previous_payload_bytes"] == 0

        second = json.loads(_call("handover", "create", body="SECOND STATE", to="target-Z", json_output=True))

        assert second["updated_existing"] is True
        assert second["previous_payload_bytes"] > 0
        assert second["handover_id"] == first["handover_id"]

    def test_the_absorb_is_announced_on_the_human_channel(self) -> None:
        _call("handover", "create", body="FIRST STATE", to="target-Z", json_output=True)
        out = _call_human("handover", "create", body="SECOND STATE", to="target-Z")
        assert "one row per session" in out
        assert "absorbed" in out.lower()


class TestCompletenessIsAssertedBeforeAnyOkLine(_PinnedSessionTestCase):
    """Verify-by-re-read: the row is re-fetched from the DB, never trusted in memory (#4194)."""

    def test_a_persisted_payload_that_lost_the_resolved_bytes_fails_loudly(self) -> None:
        def _gut_the_row(_handover, _records) -> pathlib.Path:
            SessionHandover.objects.update(payload="something else entirely")
            return pathlib.Path("/tmp/mirror.md")

        self._patch("teatree.core.management.commands.handover.upsert_subagent_section", _gut_the_row)

        err = StringIO()
        out = StringIO()
        with pytest.raises(SystemExit) as excinfo:
            call_command("handover", "create", to="target-Z", stdout=out, stderr=err, json_output=True)

        assert excinfo.value.code == 1
        data = json.loads(out.getvalue())
        assert data["ok"] is False
        assert data["completeness_ok"] is False
        assert data["completeness_failures"], "the failure must name what is wrong with the row"
        assert "OK " not in err.getvalue()

    def test_a_healthy_row_reports_completeness_ok(self) -> None:
        data = json.loads(_call("handover", "create", to="target-Z", json_output=True))
        assert data["completeness_ok"] is True
        assert data["payload_bytes"] > 0


class TestHandoverWhoami(_PinnedSessionTestCase):
    def test_whoami_prints_session_id(self) -> None:
        assert _call_human("handover", "whoami").strip() == "this-session"

    def test_whoami_json(self) -> None:
        assert json.loads(_call("handover", "whoami", json_output=True))["session_id"] == "this-session"


class TestHandoverClaimOnStart(_PinnedSessionTestCase):
    def test_claims_handover_targeted_at_session(self) -> None:
        SessionHandover.objects.create_handover(from_session="other", to_session="this-session", payload="BODY")
        out = _call("handover", "claim-on-start", session="this-session", json_output=True)
        data = json.loads(out)
        assert data["claimed"] is True
        assert data["payload"] == "BODY"
        assert data["from_session"] == "other"
        assert SessionHandover.objects.get().claimed_by == "this-session"

    def test_claim_is_idempotent_single_use(self) -> None:
        SessionHandover.objects.create_handover(from_session="other", to_session="this-session", payload="BODY")
        assert json.loads(_call("handover", "claim-on-start", session="this-session", json_output=True))["claimed"]
        assert not json.loads(_call("handover", "claim-on-start", session="this-session", json_output=True))["claimed"]

    def test_nothing_to_claim(self) -> None:
        assert json.loads(_call("handover", "claim-on-start", session="fresh", json_output=True))["claimed"] is False


class TestLoopWhoamiAndOwnerDisplay(_PinnedSessionTestCase):
    def test_loop_owner_whoami_prints_session_id(self) -> None:
        assert _call_human("loop_owner", "whoami").splitlines()[0].strip() == "this-session"

    def test_loop_owner_shows_you_are(self) -> None:
        out = _call_human("loop_owner", "owner")
        assert "you are: this-session" in out

    def test_loop_owner_json_includes_you_and_owner_flag(self) -> None:
        LoopLease.objects.claim_ownership("t3-master", session_id="this-session", owner_pid=os.getpid())
        data = json.loads(_call("loop_owner", "owner", json_output=True))
        assert data["you"] == "this-session"
        assert data["you_are_owner"] is True


class TestEmptyHandoverIsRefused(_PinnedSessionTestCase):
    """A hand-off with nothing durable to transfer fails loud, never reports OK (#3551).

    And it writes NOTHING (#4194): a zero-agent barrier result is a negative fact
    ABOUT a hand-off, not a hand-off. A row carrying only "no sub-agents had pending
    work" arrives under the ``SESSION HAND-OFF RECEIVED`` directive while transferring
    nothing a receiver can act on, and it consumes the author's single unclaimed slot.
    """

    def setUp(self) -> None:
        super().setUp()
        (self.tmp_path / "state" / "t3-snapshot-this-session-precompact.md").unlink()

    def _refused(self, *args: str, **kwargs) -> tuple[dict, str]:
        """Run an EMPTY ``create``, assert it exits 1, and return its JSON + stderr."""
        out, err = StringIO(), StringIO()
        with pytest.raises(SystemExit) as excinfo:
            call_command("handover", "create", *args, stdout=out, stderr=err, json_output=True, **kwargs)
        assert excinfo.value.code == 1
        return json.loads(out.getvalue()), err.getvalue()

    def test_exits_non_zero_when_no_snapshot_and_no_live_state(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _call("handover", "create", json_output=True)

        assert excinfo.value.code == 1

    def test_an_empty_hand_off_writes_no_row(self) -> None:
        data, err = self._refused()

        assert SessionHandover.objects.count() == 0, "an empty hand-off is not durable state; nothing is persisted"
        assert data["ok"] is False
        assert data["handover_id"] is None
        assert data["row_written"] is False
        assert data["mirror_path"] == ""
        assert list(self.tmp_path.rglob("handover-*.md")) == [], "no row means no mirror either"
        assert "No row was written" in err

    def test_an_empty_hand_off_delivers_nothing_to_a_receiver(self) -> None:
        """The end-to-end statement: a receiving session is handed nothing at all."""
        self._refused()

        assert claim_handovers("some-receiving-session") == ("", "")

    def test_an_empty_hand_off_leaves_an_existing_unclaimed_row_untouched(self) -> None:
        SessionHandover.objects.create_handover(from_session="this-session", to_session="first", payload="REAL STATE")
        before = SessionHandover.objects.get()

        self._refused(to="other")

        after = SessionHandover.objects.get()
        assert after.payload == before.payload
        assert after.to_session == before.to_session
        assert after.created_at == before.created_at

    def test_an_empty_hand_off_still_runs_the_barrier_and_reports_it(self) -> None:
        """Rescuing stranded sub-agent work is orthogonal to whether a payload exists."""
        outcome = FastPushOutcome(ok=True, branch="feat/x", committed=True, pushed=True, pr_url="http://pr/1")
        push = SubagentPush(worktree=pathlib.Path("/wt/agent-x"), branch="feat/x", driven=True, outcome=outcome)
        self._patch("teatree.core.management.commands.handover.drive_subagents_to_fast_push", lambda *a, **k: [push])

        data, _err = self._refused()

        assert data["subagent_count"] == 1
        assert data["subagent_pushes"][0]["pr_url"] == "http://pr/1"
        assert SessionHandover.objects.count() == 0


class TestTheJsonAndExitCodesAreOtherwiseUnchanged(_PinnedSessionTestCase):
    """Control test — GREEN before AND after; a red here means a contract broke."""

    def _code(self, *args: str, **kwargs) -> int:
        with pytest.raises(SystemExit) as excinfo:
            call_command("handover", "create", *args, stdout=StringIO(), stderr=StringIO(), **kwargs)
        return int(excinfo.value.code or 0)

    def test_an_authored_body_exits_zero(self) -> None:
        assert json.loads(_call("handover", "create", body="AUTHORED", json_output=True))["ok"] is True

    def test_a_live_state_payload_is_unvetted_and_exits_three(self) -> None:
        (self.tmp_path / "state" / "t3-snapshot-this-session-precompact.md").unlink()
        Ticket.objects.create(issue_url="https://github.com/o/r/issues/1", short_description="real work")

        assert self._code(json_output=True) == 3

    def test_a_self_addressed_target_exits_one(self) -> None:
        assert self._code(to="this-session", json_output=True) == 1

    def test_both_payload_inputs_exit_two(self) -> None:
        assert self._code(body="B", from_file="/nonexistent", json_output=True) == 2
