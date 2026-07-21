"""The non-negotiable invariants the availability+preset merge must not regress (#61).

Locks the five owner-flagged invariants around the merged :class:`LoopPreset` (Mode):

1.  Owner-reply ALWAYS-ON — the reactive owner-DM reply recorder never consults the
    merged-mode resolver / defer predicate (static guard; the functional proof lives
    in ``tests/teatree_agents/test_owner_answer_threading.py``).
2.  Auto-merge under away — loop membership, proven in
    ``tests/teatree_loops/test_loop_table.py::TestAutoMergePathAdmittedUnderAutonomousAway``.
3.  Live-presence #189 escape — ``PresenceHeartbeat.is_live_user_turn`` still gates a
    per-turn in-client render, independent of the named mode.
4.  autoload gate — untouched by the merge (no mode read added to it).
5.  ``require_human_approval_to_merge`` stays a SEPARATE knob — it is NOT folded into
    the merged Mode (design decision D).
"""

import datetime as dt
from datetime import UTC, datetime, timedelta
from pathlib import Path

import django.test

from teatree.core.availability import PresenceHeartbeat
from teatree.core.models import LoopPreset


class TestOwnerReplyAlwaysOn(django.test.SimpleTestCase):
    """Invariant 1: the owner-reply recorder must never grow a mode/defer gate."""

    _FORBIDDEN = ("resolve_active_mode", "mode_resolution", "resolve_mode", "defers_questions")

    def test_recorder_source_never_reads_the_mode(self) -> None:
        import teatree.agents.reactive_envelope_recorders as recorders  # noqa: PLC0415 — test-time module inspection

        source = Path(recorders.__file__).read_text(encoding="utf-8")
        offenders = [token for token in self._FORBIDDEN if token in source]
        assert offenders == [], f"owner-reply recorder must stay mode-independent — found: {offenders}"


class TestRequireHumanApprovalStaysSeparate(django.test.SimpleTestCase):
    """Invariant 5: the merge-approval knob is NOT an attribute of the merged Mode."""

    def test_mode_has_no_merge_approval_field(self) -> None:
        field_names = {field.name for field in LoopPreset._meta.get_fields()}
        assert "require_human_approval_to_merge" not in field_names
        # The merged Mode carries only the loop mask + the three availability booleans.
        assert {"defers_questions", "pauses_self_pump", "presence_sensitive"} <= field_names
        assert not any("approval" in name or "merge" in name for name in field_names)


class TestLivePresenceEscapeIntact(django.test.SimpleTestCase):
    """Invariant 3: the #189 per-turn live-user escape is unchanged by the merge."""

    def _heartbeat(self, tmp: Path) -> PresenceHeartbeat:
        return PresenceHeartbeat(locate=lambda: tmp / "presence")

    def test_fresh_same_session_turn_is_live(self) -> None:
        import tempfile  # noqa: PLC0415 — test-local

        with tempfile.TemporaryDirectory() as td:
            beat = self._heartbeat(Path(td))
            now = datetime.now(tz=UTC)
            beat.record(session_id="sess-A", now=now)
            assert beat.is_live_user_turn(session_id="sess-A", now=now + timedelta(seconds=10)) is True

    def test_foreign_session_is_not_live(self) -> None:
        import tempfile  # noqa: PLC0415 — test-local

        with tempfile.TemporaryDirectory() as td:
            beat = self._heartbeat(Path(td))
            now = datetime.now(tz=UTC)
            beat.record(session_id="sess-A", now=now)
            assert beat.is_live_user_turn(session_id="sess-B", now=now) is False

    def test_stale_turn_is_not_live(self) -> None:
        import tempfile  # noqa: PLC0415 — test-local

        with tempfile.TemporaryDirectory() as td:
            beat = self._heartbeat(Path(td))
            now = datetime.now(tz=UTC)
            beat.record(session_id="sess-A", now=now - dt.timedelta(minutes=5))
            assert beat.is_live_user_turn(session_id="sess-A", now=now) is False
