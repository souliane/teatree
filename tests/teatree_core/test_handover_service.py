"""Tests for the session hand-off service.

Covers reuse of the PreCompact snapshot file as the payload, target
resolution (explicit id, live loop owner, parked-for-next), and the XDG
file mirror.
"""

import contextlib
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

from django.test import TestCase

from teatree.core import handover
from teatree.core.models import LoopLease, SessionHandover


@contextlib.contextmanager
def _tmp_env(var: str) -> Iterator[str]:
    """Set ``var`` to a fresh temp dir for the duration, restoring the prior value."""
    prior = os.environ.get(var)
    with tempfile.TemporaryDirectory() as directory:
        os.environ[var] = directory
        try:
            yield directory
        finally:
            if prior is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = prior


class TestSnapshotPayloadReuse(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.state_dir = Path(self.enterContext(_tmp_env("TEATREE_CLAUDE_STATUSLINE_STATE_DIR")))

    def test_reads_precompact_snapshot_file_as_payload(self) -> None:
        (self.state_dir / "t3-snapshot-sess-A-precompact.md").write_text("SNAPSHOT BODY", encoding="utf-8")
        assert handover.snapshot_payload("sess-A") == "SNAPSHOT BODY"

    def test_falls_back_to_stub_when_no_snapshot(self) -> None:
        payload = handover.snapshot_payload("sess-never-compacted")
        assert "sess-never-compacted" in payload
        assert "No PreCompact" in payload


class TestResolveTargetSession(TestCase):
    def test_explicit_target_wins(self) -> None:
        assert handover.resolve_target_session("explicit-id") == "explicit-id"

    def test_no_target_resolves_to_live_loop_owner(self) -> None:
        LoopLease.objects.claim_ownership("t3-master", session_id="owner-X", owner_pid=os.getpid())
        assert handover.resolve_target_session("") == "owner-X"

    def test_no_target_no_live_owner_parks_for_next(self) -> None:
        assert handover.resolve_target_session("") == ""


class TestWriteMirror(TestCase):
    def setUp(self) -> None:
        super().setUp()
        # ``pointer`` is the well-known ``latest`` path; content lands in a unique sibling.
        self.pointer = Path(self.enterContext(_tmp_env("XDG_STATE_HOME"))) / "latest.md"

    def test_mirror_writes_payload_to_unique_file_and_repoints_latest(self) -> None:
        row = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="BODY")
        written = handover.write_mirror(row, self.pointer)
        # Content lives in a UNIQUE per-session file, not the fixed pointer.
        assert written != self.pointer
        assert written.name.startswith("handover-")
        assert "a" in written.name
        text = written.read_text(encoding="utf-8")
        assert "from: `a`" in text
        assert "to: `b`" in text
        assert "BODY" in text
        # ``latest`` resolves to the same content the unique file holds.
        assert self.pointer.read_text(encoding="utf-8") == text

    def test_next_session_renders_as_next_session(self) -> None:
        row = SessionHandover.objects.create_handover(from_session="a", to_session="", payload="BODY")
        written = handover.write_mirror(row, self.pointer)
        assert "to: `next-session`" in written.read_text(encoding="utf-8")


class TestUniqueMirrorNoClobber(TestCase):
    """Directive #7 — concurrent hand-offs from different sessions must not clobber."""

    def setUp(self) -> None:
        super().setUp()
        self.pointer = Path(self.enterContext(_tmp_env("XDG_STATE_HOME"))) / "latest.md"

    def test_two_concurrent_handoffs_write_distinct_files(self) -> None:
        first = SessionHandover.objects.create_handover(from_session="sess-A", to_session="x", payload="FROM-A")
        second = SessionHandover.objects.create_handover(from_session="sess-B", to_session="y", payload="FROM-B")

        first_file = handover.write_mirror(first, self.pointer)
        second_file = handover.write_mirror(second, self.pointer)

        assert first_file != second_file
        assert first_file.read_text(encoding="utf-8").find("FROM-A") != -1
        assert second_file.read_text(encoding="utf-8").find("FROM-B") != -1
        # The first session's mirror survived the second hand-off (no clobber).
        assert "FROM-A" in first_file.read_text(encoding="utf-8")

    def test_latest_pointer_tracks_the_newest_handover(self) -> None:
        first = SessionHandover.objects.create_handover(from_session="sess-A", to_session="x", payload="FROM-A")
        second = SessionHandover.objects.create_handover(from_session="sess-B", to_session="y", payload="FROM-B")

        handover.write_mirror(first, self.pointer)
        handover.write_mirror(second, self.pointer)

        assert "FROM-B" in self.pointer.read_text(encoding="utf-8")

    def test_remirroring_same_row_is_idempotent(self) -> None:
        row = SessionHandover.objects.create_handover(from_session="sess-A", to_session="x", payload="BODY")
        assert handover.write_mirror(row, self.pointer) == handover.write_mirror(row, self.pointer)


class TestCreateHandover(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.state_dir = Path(self.enterContext(_tmp_env("TEATREE_CLAUDE_STATUSLINE_STATE_DIR")))
        self.enterContext(_tmp_env("XDG_STATE_HOME"))

    def test_create_persists_row_and_mirror_to_loop_owner(self) -> None:
        LoopLease.objects.claim_ownership("t3-master", session_id="owner-X", owner_pid=os.getpid())
        row, mirror = handover.create_handover(from_session="hand-er", explicit_to="")
        assert row.to_session == "owner-X"
        assert SessionHandover.objects.filter(pk=row.pk).exists()
        assert mirror.is_file()
        assert "hand-er" in mirror.read_text(encoding="utf-8")

    def test_create_with_explicit_to_targets_that_session(self) -> None:
        row, _ = handover.create_handover(from_session="hand-er", explicit_to="target-Z")
        assert row.to_session == "target-Z"

    def test_create_no_owner_parks_for_next(self) -> None:
        row, _ = handover.create_handover(from_session="hand-er", explicit_to="")
        assert row.to_session == ""
        assert row.is_for_next_session is True
