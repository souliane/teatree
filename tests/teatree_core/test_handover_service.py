"""Tests for the session hand-off service.

Covers reuse of the PreCompact snapshot file as the payload, target
resolution (explicit id, live loop owner, parked-for-next), the XDG
file mirror, and the sub-agent wrap-up section the barrier's returns become.
"""

import contextlib
import datetime as dt
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

from django.test import TestCase

from teatree.core import handover
from teatree.core.fast_push import FastPushOutcome, LeakFinding
from teatree.core.handover import resolve_handover
from teatree.core.handover_orchestration import SubagentPush
from teatree.core.handover_wrapup import (
    merge_subagent_records,
    render_subagent_section,
    subagent_record,
    upsert_subagent_section,
)
from teatree.core.models import LoopLease, SessionHandover
from teatree.core.session_handover_manager import SessionHandoverQuerySet


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
        assert handover.HandoverPayload("sess-A").snapshot() == "SNAPSHOT BODY"

    def test_no_snapshot_reads_empty_so_the_caller_derives_live_state(self) -> None:
        # #3551: the stub that told the receiver to re-derive everything is gone.
        # An absent snapshot yields "", and ``resolve()`` falls through to ``live_state()``.
        assert handover.HandoverPayload("sess-never-compacted").snapshot() == ""

    def test_handover_payload_prefers_the_snapshot(self) -> None:
        (self.state_dir / "t3-snapshot-sess-B-precompact.md").write_text("SNAPSHOT BODY", encoding="utf-8")
        resolved = handover.HandoverPayload("sess-B").resolve()
        assert resolved.text == "SNAPSHOT BODY"
        assert resolved.source is handover.PayloadSource.SNAPSHOT

    def test_an_authored_body_outranks_the_snapshot(self) -> None:
        """A hand-off carries the session's reasoning; a re-derivable snapshot never displaces it."""
        (self.state_dir / "t3-snapshot-sess-C-precompact.md").write_text("SNAPSHOT BODY", encoding="utf-8")
        resolved = handover.HandoverPayload("sess-C", authored="AUTHORED BODY").resolve()
        assert resolved.text == "AUTHORED BODY"
        assert resolved.source is handover.PayloadSource.AUTHORED


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
        row = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="BODY").row
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
        row = SessionHandover.objects.create_handover(from_session="a", to_session="", payload="BODY").row
        written = handover.write_mirror(row, self.pointer)
        assert "to: `next-session`" in written.read_text(encoding="utf-8")


class TestUniqueMirrorNoClobber(TestCase):
    """Directive #7 — concurrent hand-offs from different sessions must not clobber."""

    def setUp(self) -> None:
        super().setUp()
        self.pointer = Path(self.enterContext(_tmp_env("XDG_STATE_HOME"))) / "latest.md"

    def test_two_concurrent_handoffs_write_distinct_files(self) -> None:
        first = SessionHandover.objects.create_handover(from_session="sess-A", to_session="x", payload="FROM-A").row
        second = SessionHandover.objects.create_handover(from_session="sess-B", to_session="y", payload="FROM-B").row

        first_file = handover.write_mirror(first, self.pointer)
        second_file = handover.write_mirror(second, self.pointer)

        assert first_file != second_file
        assert first_file.read_text(encoding="utf-8").find("FROM-A") != -1
        assert second_file.read_text(encoding="utf-8").find("FROM-B") != -1
        # The first session's mirror survived the second hand-off (no clobber).
        assert "FROM-A" in first_file.read_text(encoding="utf-8")

    def test_latest_pointer_tracks_the_newest_handover(self) -> None:
        first = SessionHandover.objects.create_handover(from_session="sess-A", to_session="x", payload="FROM-A").row
        second = SessionHandover.objects.create_handover(from_session="sess-B", to_session="y", payload="FROM-B").row

        handover.write_mirror(first, self.pointer)
        handover.write_mirror(second, self.pointer)

        assert "FROM-B" in self.pointer.read_text(encoding="utf-8")

    def test_remirroring_same_row_is_idempotent(self) -> None:
        row = SessionHandover.objects.create_handover(from_session="sess-A", to_session="x", payload="BODY").row
        assert handover.write_mirror(row, self.pointer) == handover.write_mirror(row, self.pointer)


class TestCreateHandover(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.state_dir = Path(self.enterContext(_tmp_env("TEATREE_CLAUDE_STATUSLINE_STATE_DIR")))
        self.enterContext(_tmp_env("XDG_STATE_HOME"))

    def test_create_persists_row_and_mirror_to_loop_owner(self) -> None:
        LoopLease.objects.claim_ownership("t3-master", session_id="owner-X", owner_pid=os.getpid())
        created = handover.create_handover(
            from_session="hand-er", resolution=resolve_handover(from_session="hand-er", explicit_to="")
        )
        row, mirror = created.handover, created.mirror
        assert row.to_session == "owner-X"
        assert SessionHandover.objects.filter(pk=row.pk).exists()
        assert mirror.is_file()
        assert "hand-er" in mirror.read_text(encoding="utf-8")

    def test_create_with_explicit_to_targets_that_session(self) -> None:
        row = handover.create_handover(
            from_session="hand-er", resolution=resolve_handover(from_session="hand-er", explicit_to="target-Z")
        ).handover
        assert row.to_session == "target-Z"

    def test_create_no_owner_parks_for_next(self) -> None:
        row = handover.create_handover(
            from_session="hand-er", resolution=resolve_handover(from_session="hand-er", explicit_to="")
        ).handover
        assert row.to_session == ""
        assert row.is_for_next_session is True


_AT = dt.datetime(2026, 8, 4, 12, 0, tzinfo=dt.UTC)
_LATER = dt.datetime(2026, 8, 4, 13, 0, tzinfo=dt.UTC)


def _records(pushes: "list[SubagentPush]", *, at: dt.datetime) -> list[dict]:
    return [subagent_record(push, at=at) for push in pushes]


class TestRenderSubagentSection:
    """What the barrier collected, as the section the receiver reads (#4194)."""

    def test_each_agent_contributes_what_it_finished_and_what_is_left(self) -> None:
        pushed = SubagentPush(
            worktree=Path("/wt/agent-x"),
            branch="feat/x",
            driven=True,
            outcome=FastPushOutcome(ok=True, branch="feat/x", committed=True, pushed=True, pr_url="http://pr/1"),
        )
        refused = SubagentPush(
            worktree=Path("/wt/agent-y"),
            branch="feat/y",
            driven=True,
            outcome=FastPushOutcome(
                ok=False, branch="feat/y", findings=[LeakFinding(gate="secrets", path="a.py", detail="token in a.py")]
            ),
        )

        section = render_subagent_section(_records([pushed, refused], at=_AT))

        assert "Sub-agent wrap-up (2 agents seen; 2 enumerated at the latest barrier)" in section
        assert "committed, pushed, PR http://pr/1" in section
        assert "token in a.py" in section

    def test_a_driven_agent_with_no_outcome_says_so_rather_than_reading_as_done(self) -> None:
        """``driven`` with no ``outcome`` is a hole in the barrier's own report, not a clean push."""
        section = render_subagent_section(
            _records([SubagentPush(worktree=Path("/wt/agent-q"), branch="feat/q", driven=True)], at=_AT)
        )
        assert "no outcome was recorded" in section

    def test_an_undriven_agent_reports_its_error_as_what_remains(self) -> None:
        section = render_subagent_section(
            _records(
                [SubagentPush(worktree=Path("/wt/agent-z"), branch="feat/z", driven=False, error="git exploded")],
                at=_AT,
            )
        )
        assert "git exploded" in section

    def test_zero_agents_render_an_explicit_line(self) -> None:
        """An absent section is indistinguishable from a barrier that never ran."""
        assert "No in-flight sub-agent worktrees" in render_subagent_section([])


class TestMergeSubagentRecords:
    """One record per agent worktree, carrying every agent the row's barriers ever saw."""

    def test_merge_keeps_an_agent_seen_only_in_an_earlier_barrier(self) -> None:
        first = _records(
            [
                SubagentPush(worktree=Path("/wt/a"), branch="feat/a", driven=False, error="refused: unpushed"),
                SubagentPush(worktree=Path("/wt/b"), branch="feat/b", driven=False, error="refused: leak"),
            ],
            at=_AT,
        )
        second = _records(
            [
                SubagentPush(
                    worktree=Path("/wt/b"),
                    branch="feat/b2",
                    driven=True,
                    outcome=FastPushOutcome(ok=True, branch="feat/b2", committed=True, pushed=True),
                )
            ],
            at=_LATER,
        )

        merged = merge_subagent_records(first, second)

        assert [record["worktree"] for record in merged] == ["/wt/a", "/wt/b"]
        gone, still_there = merged
        assert gone["in_latest_barrier"] is False, "absence from the latest barrier may mean its worktree is gone"
        assert gone["remaining"] == "refused: unpushed", "its LAST KNOWN status is kept, not blanked"
        assert gone["first_seen_at"] == gone["last_seen_at"] == _AT.isoformat()
        assert still_there["in_latest_barrier"] is True
        assert still_there["branch"] == "feat/b2"
        assert still_there["remaining"] == "nothing"
        assert still_there["first_seen_at"] == _AT.isoformat(), "the first sighting survives the update"
        assert still_there["last_seen_at"] == _LATER.isoformat()

    def test_an_agent_that_is_gone_is_marked_in_the_rendered_section(self) -> None:
        merged = merge_subagent_records(
            _records([SubagentPush(worktree=Path("/wt/a"), branch="feat/a", driven=False, error="boom")], at=_AT),
            _records([SubagentPush(worktree=Path("/wt/c"), branch="feat/c", driven=False, error="boom")], at=_LATER),
        )

        section = render_subagent_section(merged)

        assert "Sub-agent wrap-up (2 agents seen; 1 enumerated at the latest barrier)" in section
        assert "NOT enumerated at the latest barrier" in section

    def test_merging_into_nothing_is_the_incoming_set(self) -> None:
        incoming = _records([SubagentPush(worktree=Path("/wt/a"), branch="feat/a", driven=False, error="x")], at=_AT)
        assert merge_subagent_records([], incoming) == incoming


class TestResolveHandover(TestCase):
    """Where a hand-off would go and what it would carry, decided BEFORE anything is written."""

    def setUp(self) -> None:
        super().setUp()
        self.state_dir = Path(self.enterContext(_tmp_env("TEATREE_CLAUDE_STATUSLINE_STATE_DIR")))
        self.enterContext(_tmp_env("XDG_STATE_HOME"))

    def test_an_authored_body_resolves_to_its_target_and_source_without_writing(self) -> None:
        resolution = resolve_handover(from_session="hand-er", explicit_to="target-Z", authored="BODY")

        assert resolution.to_session == "target-Z"
        assert resolution.resolved.text == "BODY"
        assert resolution.resolved.source is handover.PayloadSource.AUTHORED
        assert SessionHandover.objects.count() == 0, "resolving must not persist anything"

    def test_nothing_anywhere_resolves_empty_and_still_writes_nothing(self) -> None:
        resolution = resolve_handover(from_session="hand-er", explicit_to="")

        assert resolution.resolved.source is handover.PayloadSource.EMPTY
        assert SessionHandover.objects.count() == 0


class TestTheAbsorbIsReportedFromTheWriteSeam(TestCase):
    """The caller reports what the write DID, never what a pre-read predicted it would do."""

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(_tmp_env("XDG_STATE_HOME"))

    def test_an_absorb_that_lands_via_the_integrity_retry_is_still_reported(self) -> None:
        """A rival row inserted between the pre-read and the insert made the absorb silent.

        The caller's pre-read saw nothing, so a call that absorbed 11 bytes reported
        ``updated_existing: false, previous_payload_bytes: 0`` — silent in exactly the
        way those two fields exist to prevent.
        """
        original = SessionHandoverQuerySet._unclaimed_for
        seen: list[int] = []

        def _rival_wins_the_insert(self: SessionHandoverQuerySet, from_session: str) -> "SessionHandover | None":
            seen.append(1)
            if len(seen) == 1:
                SessionHandover.objects.create(from_session="a", to_session="b", payload="RIVAL-STATE")
                return None
            return original(self, from_session)

        with mock.patch.object(SessionHandoverQuerySet, "_unclaimed_for", _rival_wins_the_insert):
            created = handover.create_handover(
                from_session="a", resolution=resolve_handover(from_session="a", explicit_to="b", authored="MY-STATE")
            )

        payload = SessionHandover.objects.get().payload
        assert "RIVAL-STATE" in payload
        assert "MY-STATE" in payload
        assert created.updated_existing is True
        assert created.previous_bytes == len("RIVAL-STATE")


class TestUpsertSubagentSection(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(_tmp_env("XDG_STATE_HOME"))

    def test_the_section_is_persisted_and_the_same_mirror_file_is_rewritten(self) -> None:
        created = handover.create_handover(
            from_session="hand-er",
            resolution=resolve_handover(from_session="hand-er", explicit_to="target-Z", authored="BODY"),
        )

        rewritten = upsert_subagent_section(created.handover, [])

        assert rewritten == created.mirror, "created_at is untouched, so the unique mirror file is the same one"
        row = SessionHandover.objects.get(pk=created.handover.pk)
        assert row.payload.startswith("BODY")
        assert "Sub-agent wrap-up" in row.payload
        assert "Sub-agent wrap-up" in rewritten.read_text(encoding="utf-8")

    def test_the_records_are_stored_on_the_row_rather_than_re_parsed_from_the_payload(self) -> None:
        created = handover.create_handover(
            from_session="hand-er",
            resolution=resolve_handover(from_session="hand-er", explicit_to="target-Z", authored="BODY"),
        )
        records = _records([SubagentPush(worktree=Path("/wt/a"), branch="feat/a", driven=False, error="x")], at=_AT)

        upsert_subagent_section(created.handover, records)

        assert SessionHandover.objects.get(pk=created.handover.pk).subagent_wrapup == records
