"""Tests for the ``TaskCreated`` deny surface (#4216).

The harness's task-creation consumer reads exactly one field of a hook result:
the ``blockingError`` its runner derives. ``{"continue": false, "stopReason":
…}`` becomes ``preventContinuation``, which that consumer never looks at — so a
deny carrying only the teammate-stop envelope surfaced the exit-2 stderr
fallback (``[<command>]: No stderr output``) instead of the reason, leaving a
block with no remedy and nothing to distinguish it from a crashed hook.

``harness_surfaced_deny_text`` models that read, and the emitter enforces it:
a reason that would not reach the caller fails OPEN and logs rather than
blocking silently.
"""

import inspect
import io
import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import hooks.scripts.hook_router as router
from hooks.scripts.task_created_deny import (
    DENY_EXIT_CODE,
    build_deny_payload,
    emit_task_create_deny,
    harness_surfaced_deny_text,
)

_REASON = "SKILL LOADING ENFORCEMENT (TaskCreated): add `Read skills/code/SKILL.md` to the prompt."


def _emit(reason: str) -> tuple[bool, dict | None, str]:
    """Run the emitter, returning ``(blocked, stdout_payload, stderr_text)``."""
    out = StringIO()
    err = io.StringIO()
    with patch("sys.stdout", out), patch("sys.stderr", err):
        blocked = emit_task_create_deny(reason)
    raw = out.getvalue().strip()
    return blocked, json.loads(raw) if raw else None, err.getvalue()


class TestTheReasonReachesTheCaller:
    """The emitted deny surfaces its reason, not an empty failure."""

    def test_surfaced_text_is_the_reason(self) -> None:
        blocked, payload, _ = _emit(_REASON)
        assert blocked is True
        assert payload is not None
        assert harness_surfaced_deny_text(payload, "", exit_code=DENY_EXIT_CODE) == _REASON

    def test_teammate_stop_envelope_is_preserved(self) -> None:
        _, payload, _ = _emit(_REASON)
        assert payload is not None
        assert payload["continue"] is False
        assert payload["stopReason"] == _REASON
        assert "permissionDecision" not in payload

    def test_stop_reason_alone_surfaces_nothing(self) -> None:
        # The pre-fix envelope: the consumer reads no ``blockingError`` from it, so
        # an exit-2 deny with an empty stderr is the "No stderr output" block.
        legacy = {"continue": False, "stopReason": _REASON}
        assert harness_surfaced_deny_text(legacy, "", exit_code=DENY_EXIT_CODE) == ""

    def test_stderr_is_the_fallback_channel_when_no_block_decision(self) -> None:
        assert harness_surfaced_deny_text({}, " boom \n", exit_code=DENY_EXIT_CODE) == "boom"

    def test_nothing_surfaces_below_the_deny_exit_code(self) -> None:
        assert harness_surfaced_deny_text({}, "boom", exit_code=0) == ""


class TestUnsurfaceableReasonFailsOpen:
    """A deny whose reason cannot reach the caller allows and logs instead."""

    def test_blank_reason_allows(self) -> None:
        blocked, payload, _ = _emit("   \n ")
        assert blocked is False
        assert payload is None

    def test_blank_reason_logs(self) -> None:
        _, _, stderr = _emit("")
        assert "TaskCreated" in stderr
        assert stderr.strip() != ""

    def test_payload_that_drops_the_reason_fails_open(self) -> None:
        # The invariant is enforced against the payload actually built, so a future
        # envelope change that stops carrying the reason degrades to an allow.
        with patch("hooks.scripts.task_created_deny.build_deny_payload", return_value={"continue": False}):
            blocked, payload, stderr = _emit(_REASON)
        assert blocked is False
        assert payload is None
        assert stderr.strip() != ""


class TestPayloadShape:
    """The built payload carries both the stop envelope and the read field."""

    def test_reason_is_stripped(self) -> None:
        assert build_deny_payload(f"  {_REASON}  ")["reason"] == _REASON

    def test_block_decision_is_the_read_field(self) -> None:
        assert build_deny_payload(_REASON)["decision"] == "block"


class TestTheSiblingEventIsUnclaimed:
    """``TaskCompleted`` shares the schema, so the mismatch could reach it — nothing claims it."""

    def test_no_task_completed_handler_is_registered(self) -> None:
        assert "TaskCompleted" not in router._HANDLERS

    def test_every_task_created_handler_denies_through_this_emitter(self) -> None:
        modules = [Path(inspect.getfile(h)) for h in router._HANDLERS["TaskCreated"]]
        assert all("emit_task_create_deny" in m.read_text(encoding="utf-8") for m in modules)
