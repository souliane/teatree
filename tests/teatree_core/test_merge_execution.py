"""The IN_REVIEW → MERGED keystone transition (BLUEPRINT §17.4).

These tests exercise the missing FSM transition, its §17.4.3 pre-condition
hook, the ``expected_head_oid`` SHA-binding (TOCTOU/replay defence), and the
atomic post hook (CLEAR-consume + audit + attestation + ``mark_merged()``).
Only the unstoppable external — the ``gh`` subprocess — is stubbed; every
teatree model / FSM / DB write is real.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core import merge_execution
from teatree.core.merge_execution import (
    MergeHeadMovedError,
    MergeOutcome,
    MergePreconditionError,
    MergeReplayError,
    assert_merge_preconditions,
    merge_ticket_pr,
    record_merge_and_advance,
)
from teatree.core.models import MergeAudit, MergeClear, Session, Ticket

pytestmark = pytest.mark.django_db

_SHA = "a" * 40
_MOVED = "b" * 40
_GREEN = '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]'


def _clear(ticket: Ticket, **overrides: object) -> MergeClear:
    defaults: dict[str, object] = {
        "ticket": ticket,
        "pr_id": 859,
        "slug": "souliane/teatree",
        "reviewed_sha": _SHA,
        "reviewer_identity": "cold-reviewer",
        "gh_verify_result": MergeClear.VerifyResult.GREEN,
        "blast_class": MergeClear.BlastClass.DOCS,
    }
    defaults.update(overrides)
    return MergeClear.objects.create(**defaults)


class _GhStub:
    """Records argv and returns scripted ``gh`` responses keyed by subcommand."""

    def __init__(self, *, head: str = _SHA, draft: str = "false", checks: str = _GREEN, merge_rc: int = 0) -> None:
        self.head = head
        self.draft = draft
        self.checks = checks
        self.merge_rc = merge_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        joined = " ".join(argv)
        if "headRefOid" in joined:
            return (0, self.head, "")
        if "isDraft" in joined:
            return (0, self.draft, "")
        if "statusCheckRollup" in joined:
            return (0, self.checks, "")
        if "pulls" in joined and "merge" in joined:
            if self.merge_rc != 0:
                return (1, "", "Head branch was modified. Review and try the merge again. (409)")
            return (0, '{"sha": "merged0deadbeef"}', "")
        return (0, "", "")


def _run(clear: MergeClear, stub: _GhStub, identity: str = "merge-loop") -> MergeOutcome:
    with patch("teatree.core.merge_execution._run_gh", side_effect=stub):
        return merge_ticket_pr(clear=clear, executing_loop_identity=identity)


class TestMergeKeystoneHappyPath(TestCase):
    def test_merge_advances_fsm_and_writes_audit(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        outcome = _run(clear, _GhStub())

        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert outcome.merged_sha
        assert ticket.state == Ticket.State.MERGED
        assert clear.consumed_at is not None
        audit = MergeAudit.objects.get(clear=clear)
        assert audit.required_checks_status == "green"
        session = Session.objects.filter(ticket=ticket).first()
        assert session is not None
        assert "merged" in session.visited_phases

    def test_expected_head_oid_is_bound_to_verified_sha(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        stub = _GhStub()
        _run(clear, stub)
        merge_calls = [c for c in stub.calls if "merge" in " ".join(c) and "pulls" in " ".join(c)]
        assert merge_calls
        assert f"sha={_SHA}" in merge_calls[0]


class TestMergeKeystonePreconditions(TestCase):
    def test_substrate_blast_class_is_never_auto_merged(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket, blast_class=MergeClear.BlastClass.SUBSTRATE)
        with pytest.raises(MergePreconditionError, match="substrate"):
            _run(clear, _GhStub())
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_self_issued_clear_is_refused(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket, reviewer_identity="merge-loop")
        with pytest.raises(MergePreconditionError, match="independent"):
            _run(clear, _GhStub(), identity="merge-loop")

    def test_stale_sha_is_refused(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        with pytest.raises(MergePreconditionError, match="head moved"):
            _run(clear, _GhStub(head=_MOVED))
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_draft_pr_is_refused(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        with pytest.raises(MergePreconditionError, match="draft"):
            _run(clear, _GhStub(draft="true"))

    def test_non_green_checks_refused(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        failing = '[{"status": "COMPLETED", "conclusion": "FAILURE"}]'
        with pytest.raises(MergePreconditionError, match="not green"):
            _run(clear, _GhStub(checks=failing))

    def test_consumed_clear_not_actionable(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        clear.consumed_at = timezone.now()
        clear.save(update_fields=["consumed_at"])
        with pytest.raises(MergePreconditionError, match="not actionable"):
            _run(clear, _GhStub())

    def test_head_moved_during_merge_is_fail_closed(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        with pytest.raises(MergeHeadMovedError):
            _run(clear, _GhStub(merge_rc=1))
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert not (MergeAudit.objects.filter(clear=clear).exists())


class TestMergeClearActionable(TestCase):
    def test_missing_field_is_treated_as_absent(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        assert not (_clear(ticket, reviewer_identity="").is_actionable())

    def test_fully_populated_unconsumed_is_actionable(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        assert _clear(ticket).is_actionable()

    def test_str_renders_slug_pr_and_sha(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        assert "souliane/teatree#859" in str(clear)
        audit = MergeAudit.objects.create(clear=clear, merged_sha="d" * 40, required_checks_status="green")
        assert "souliane/teatree#859" in str(audit)


class TestMergeExecutionEdgeCases(TestCase):
    def test_non_mergeclear_input_is_refused(self) -> None:
        with pytest.raises(MergePreconditionError, match="requires a MergeClear"):
            merge_ticket_pr(clear=object(), executing_loop_identity="merge-loop")

    def test_missing_live_head_sha_is_refused(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)

        def _no_head(argv: list[str]) -> tuple[int, str, str]:
            if "headRefOid" in " ".join(argv):
                return (1, "", "boom")
            return (0, "", "")

        with (
            patch("teatree.core.merge_execution._run_gh", side_effect=_no_head),
            pytest.raises(MergePreconditionError, match="could not resolve the live head"),
        ):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

    def test_generic_merge_failure_is_refused_not_head_moved(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)

        def _merge_500(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "headRefOid" in joined:
                return (0, _SHA, "")
            if "isDraft" in joined:
                return (0, "false", "")
            if "statusCheckRollup" in joined:
                return (0, _GREEN, "")
            if "pulls" in joined and "merge" in joined:
                return (1, "", "500 Internal Server Error")
            return (0, "", "")

        with (
            patch("teatree.core.merge_execution._run_gh", side_effect=_merge_500),
            pytest.raises(MergePreconditionError, match="failed"),
        ):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

    def test_malformed_rollup_json_is_failed(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        with pytest.raises(MergePreconditionError, match="not green"):
            _run(clear, _GhStub(checks="{not json"))

    def test_non_list_rollup_is_failed(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        with pytest.raises(MergePreconditionError, match="not green"):
            _run(clear, _GhStub(checks='{"a": 1}'))

    def test_pending_check_is_not_green(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        pending = '[{"status": "IN_PROGRESS"}]'
        with pytest.raises(MergePreconditionError, match="not green"):
            _run(clear, _GhStub(checks=pending))

    def test_legacy_status_context_pending_state_is_not_green(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        legacy_pending = '[{"state": "PENDING"}]'
        with pytest.raises(MergePreconditionError, match="not green"):
            _run(clear, _GhStub(checks=legacy_pending))

    def test_non_dict_rollup_entry_is_ignored(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        # A non-dict entry is skipped; the dict success entry decides green.
        mixed = '["junk", {"status": "COMPLETED", "conclusion": "SUCCESS"}]'
        outcome = _run(clear, _GhStub(checks=mixed))
        assert outcome.merged_sha

    def test_assert_preconditions_rejects_non_mergeclear(self) -> None:
        with pytest.raises(MergePreconditionError, match="no MergeClear row"):
            assert_merge_preconditions(
                clear=object(),
                executing_loop_identity="merge-loop",
                slug="souliane/teatree",
                pr_id=1,
            )

    def test_run_gh_resolves_binary_and_forwards_argv(self) -> None:
        # ``_run_gh`` resolves the ``gh`` binary via shutil.which and
        # forwards argv to the run helper. The subprocess itself is the
        # unstoppable external and is stubbed (sandboxed pre-push has no
        # executable ``gh``); the binary-resolution + argv-forwarding
        # branch is what this exercises.
        from types import SimpleNamespace  # noqa: PLC0415

        captured: list[list[str]] = []

        def _fake_run(argv: list[str], **_kw: object) -> object:
            captured.append(argv)
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        with (
            patch("teatree.core.merge_execution.shutil.which", return_value="/usr/bin/gh"),
            patch("teatree.core.merge_execution.run_allowed_to_fail", side_effect=_fake_run),
        ):
            rc, out, _err = merge_execution._run_gh(["pr", "view", "1"])
        assert rc == 0
        assert out == "ok"
        assert captured == [["/usr/bin/gh", "pr", "view", "1"]]

    def test_status_rollup_query_failure_is_not_green(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)

        def _rollup_rc1(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "headRefOid" in joined:
                return (0, _SHA, "")
            if "isDraft" in joined:
                return (0, "false", "")
            if "statusCheckRollup" in joined:
                return (1, "", "api error")
            return (0, "", "")

        with (
            patch("teatree.core.merge_execution._run_gh", side_effect=_rollup_rc1),
            pytest.raises(MergePreconditionError, match="not green"),
        ):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

    def test_record_advance_skips_mark_merged_when_state_not_in_review(self) -> None:
        # A clear whose ticket is already past MERGED (e.g. RETROSPECTED):
        # the post hook still consumes + audits but does not force a
        # backward FSM move.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        clear = _clear(ticket)
        outcome = _run(clear, _GhStub())
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.RETROSPECTED
        assert outcome.ticket_state == Ticket.State.RETROSPECTED
        assert MergeAudit.objects.filter(clear=clear).exists()

    def test_merge_response_non_json_falls_back_to_expected_head(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)

        def _bad_merge_json(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "headRefOid" in joined:
                return (0, _SHA, "")
            if "isDraft" in joined:
                return (0, "false", "")
            if "statusCheckRollup" in joined:
                return (0, _GREEN, "")
            if "pulls" in joined and "merge" in joined:
                return (0, "not-json-at-all", "")
            return (0, "", "")

        with patch("teatree.core.merge_execution._run_gh", side_effect=_bad_merge_json):
            outcome = merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")
        # Falls back to the verified expected_head_oid when the merge
        # response body is not parseable.
        assert outcome.merged_sha == _SHA

    def test_clear_without_ticket_merges_and_returns_blank_state(self) -> None:
        clear = MergeClear.objects.create(
            ticket=None,
            pr_id=900,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.DOCS,
        )
        outcome = _run(clear, _GhStub())
        assert outcome.ticket_state == ""
        clear.refresh_from_db()
        assert clear.consumed_at is not None


class TestConcurrentConsumptionReplayDefence(TestCase):
    """N1 concurrent-consumption replay defence.

    Two executors that both passed the UNLOCKED preconditions must not both
    consume the single-use CLEAR. The post hook re-asserts ``consumed_at is
    None`` under the row lock — exactly one wins.
    """

    def test_double_post_hook_consumes_once_and_second_raises(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)

        # Executor A reaches the post hook first and wins.
        state_a = record_merge_and_advance(
            clear=clear,
            merged_sha="landeddeadbeef",
            required_checks_status="green",
        )
        assert state_a == Ticket.State.MERGED

        # Executor B passed the same unlocked assert_merge_preconditions
        # (it held a stale in-memory clear) and now reaches the post hook
        # with the row already consumed — it must lose under the lock.
        with pytest.raises(MergeReplayError, match="already consumed"):
            record_merge_and_advance(
                clear=clear,
                merged_sha="landeddeadbeef",
                required_checks_status="green",
            )

        # Exactly one audit, one FSM advance — no double-merge.
        assert MergeAudit.objects.filter(clear=clear).count() == 1
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_full_keystone_twice_same_clear_second_refused(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)

        first = _run(clear, _GhStub())
        assert first.ticket_state == Ticket.State.MERGED

        with pytest.raises(MergePreconditionError):
            _run(clear, _GhStub())
        assert MergeAudit.objects.filter(clear=clear).count() == 1
