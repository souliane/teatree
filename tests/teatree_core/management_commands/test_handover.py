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

from teatree.core.fast_push import FastPushOutcome
from teatree.core.handover_orchestration import SubagentPush
from teatree.core.models import LoopLease, SessionHandover


def _call(*args: str, **kwargs) -> str:
    buf = StringIO()
    call_command(*args, stdout=buf, **kwargs)
    return buf.getvalue()


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


class TestHandoverWhoami(_PinnedSessionTestCase):
    def test_whoami_prints_session_id(self) -> None:
        assert _call("handover", "whoami").strip() == "this-session"

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
        assert _call("loop_owner", "whoami").splitlines()[0].strip() == "this-session"

    def test_loop_owner_shows_you_are(self) -> None:
        out = _call("loop_owner", "owner")
        assert "you are: this-session" in out

    def test_loop_owner_json_includes_you_and_owner_flag(self) -> None:
        LoopLease.objects.claim_ownership("t3-master", session_id="this-session", owner_pid=os.getpid())
        data = json.loads(_call("loop_owner", "owner", json_output=True))
        assert data["you"] == "this-session"
        assert data["you_are_owner"] is True


class TestEmptyHandoverIsRefused(_PinnedSessionTestCase):
    """A hand-off with nothing durable to transfer fails loud, never reports OK (#3551)."""

    def test_exits_non_zero_when_no_snapshot_and_no_live_state(self) -> None:
        (self.tmp_path / "state" / "t3-snapshot-this-session-precompact.md").unlink()

        with pytest.raises(SystemExit) as excinfo:
            _call("handover", "create", json_output=True)

        assert excinfo.value.code == 1
