"""The IN_REVIEW → MERGED keystone transition (BLUEPRINT §17.4).

These tests exercise the missing FSM transition, its §17.4.3 pre-condition
hook, the ``expected_head_oid`` SHA-binding (TOCTOU/replay defence), and the
atomic post hook (CLEAR-consume + audit + attestation + ``mark_merged()``).
Only the unstoppable external — the ``gh`` subprocess — is stubbed; every
teatree model / FSM / DB write is real.
"""

import json
from collections.abc import Callable
from unittest.mock import patch

import pytest
from django.db import OperationalError
from django.test import TestCase
from django.utils import timezone

from teatree.core.merge import (
    MergeHeadMovedError,
    MergeOutcome,
    MergePreconditionError,
    MergeReplayError,
    MergeTransientError,
    assert_merge_preconditions,
    ci_rollup,
    execute_bound_merge,
    execution,
    merge_ticket_pr,
    record_merge_and_advance,
)
from teatree.core.models import ClearRequest, MergeAudit, MergeClear, Session, Ticket

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
    with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=stub):
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

    def test_non_reviewer_role_clear_is_refused_at_merge_time(self) -> None:
        # codex #1282 finding 1 / #1283: ``MergeClear.issue()`` rejects a
        # maker/coding-agent/loop reviewer_identity at issue time, but a row
        # written directly via ``.objects.create()`` (fixture, migration,
        # ORM bypass) skips that guard. ``_assert_clear_authorized`` must
        # therefore re-check the same role classification at merge time —
        # an equality check against the executing loop alone is not enough.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket, reviewer_identity="coding-agent")
        with pytest.raises(MergePreconditionError, match=r"non-reviewer role|independent cold reviewer"):
            _run(clear, _GhStub(), identity="merge-loop")
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert clear.consumed_at is None

    def test_every_non_reviewer_role_prefix_is_refused_at_merge_time(self) -> None:
        # Every prefix in ``NON_REVIEWER_AGENT_PREFIXES`` (the shared role
        # list at ``merge_clear.py``) must be rejected. The merge-time
        # guard reads the same helper as the issue-time guard so the two
        # gates cannot drift apart (§17.8 clause 3).
        non_reviewer_identities = [
            "maker:agent-7",
            "maker-bot-3",
            "coding-agent",
            "coding-agent-9",
            "coding",
            "loop",
            "loop-merge",
        ]
        for identity in non_reviewer_identities:
            ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
            clear = _clear(ticket, reviewer_identity=identity)
            with pytest.raises(MergePreconditionError, match=r"non-reviewer role|independent cold reviewer"):
                _run(clear, _GhStub(), identity="merge-loop")
            clear.refresh_from_db()
            assert clear.consumed_at is None, f"non-reviewer {identity!r} must not consume the CLEAR"

    def test_human_authorized_on_non_substrate_clear_is_refused(self) -> None:
        # The recorded-human-approval path is substrate-only — presenting
        # --human-authorized against a logic/docs CLEAR is refused so it
        # can never bypass independent loop review (invariant 8).
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket, blast_class=MergeClear.BlastClass.LOGIC)
        stub = _GhStub()
        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=stub),
            pytest.raises(MergePreconditionError, match="substrate-only"),
        ):
            merge_ticket_pr(
                clear=clear,
                executing_loop_identity="merge-loop",
                human_authorized="owner-123",
            )
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

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
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_no_head),
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
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_merge_500),
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

    def test_gh_runner_resolves_binary_and_forwards_argv(self) -> None:
        # The merge transport's gh runner resolves the ``gh`` binary via
        # shutil.which and forwards argv to the run helper. The subprocess
        # itself is the unstoppable external and is stubbed (sandboxed pre-push
        # has no executable ``gh``); the binary-resolution + argv-forwarding
        # branch is what this exercises.
        from types import SimpleNamespace  # noqa: PLC0415

        from teatree.backends import forge_merge_rpc  # noqa: PLC0415

        captured: list[list[str]] = []

        def _fake_run(argv: list[str], **_kw: object) -> object:
            captured.append(argv)
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        with (
            patch("teatree.backends.forge_merge_rpc.shutil.which", return_value="/usr/bin/gh"),
            patch("teatree.backends.forge_merge_rpc.run_allowed_to_fail", side_effect=_fake_run),
        ):
            rc, out, _err = forge_merge_rpc.gh_runner(token="")(["pr", "view", "1"])
        assert rc == 0
        assert out == "ok"
        assert captured == [["/usr/bin/gh", "pr", "view", "1"]]

    def test_merge_in_unconfigured_provider_context_fails_loudly(self) -> None:
        # The §9 risk: a merge in a context where the backends app is not
        # registered must RAISE loudly (the fail-safe _UnconfiguredProvider's
        # build_* raise RuntimeError), never silently no-op or shell out.
        from teatree.core import backend_registry  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        real = backend_registry.get_backend_provider()
        backend_registry.register_backend_provider(backend_registry._UNCONFIGURED)
        try:
            with pytest.raises(RuntimeError, match="no backend provider registered"):
                merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")
        finally:
            backend_registry.register_backend_provider(real)

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
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_rollup_rc1),
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

    def test_record_advance_promotes_started_ticket_to_merged(self) -> None:
        """#1343: PR-merge keystone advances a ``STARTED`` ticket to ``MERGED``.

        The original guard only fired ``mark_merged()`` when the ticket was
        already at ``IN_REVIEW``/``MERGED``, so tickets whose PR landed
        while the FSM still read ``STARTED`` stayed visibly stuck at
        ``started`` on the statusline. The post hook must reconcile any
        pre-MERGED non-terminal state to ``MERGED``.
        """
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.STARTED)
        clear = _clear(ticket)
        outcome = _run(clear, _GhStub())
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED, f"PR merged but ticket stuck at {ticket.state} — #1343 regression"
        assert outcome.ticket_state == Ticket.State.MERGED
        assert MergeAudit.objects.filter(clear=clear).exists()

    def test_record_advance_promotes_every_pre_merged_state(self) -> None:
        """State-complete: PR-merge keystone advances EVERY pre-merged state to MERGED.

        Pins the contract so a future-added pre-merged state can't silently
        re-introduce the ``stale-started`` class. ``RETROSPECTED`` /
        ``DELIVERED`` / ``IGNORED`` stay where they are (covered by sibling
        skip-test).
        """
        pre_merged = [
            Ticket.State.NOT_STARTED,
            Ticket.State.SCOPED,
            Ticket.State.STARTED,
            Ticket.State.CODED,
            Ticket.State.TESTED,
            Ticket.State.REVIEWED,
            Ticket.State.SHIPPED,
            Ticket.State.IN_REVIEW,
        ]
        for idx, start_state in enumerate(pre_merged):
            ticket = Ticket.objects.create(overlay="t3-teatree", state=start_state)
            clear = _clear(ticket, pr_id=2000 + idx)
            outcome = _run(clear, _GhStub())
            ticket.refresh_from_db()
            assert ticket.state == Ticket.State.MERGED, f"PR merged but {start_state} did not advance to MERGED"
            assert outcome.ticket_state == Ticket.State.MERGED

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

        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_bad_merge_json):
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


class _LostPostHookGhStub:
    """Models a real GitHub PR whose squash-merge LANDED but whose post-hook was lost.

    First merge attempt: preconditions pass (head == reviewed_sha, green,
    not draft, not yet merged), the ``pulls/N/merge`` call SUCCEEDS at
    GitHub (the irreversible action lands). The harness then simulates the
    process dying before ``record_merge_and_advance`` consumes the CLEAR.

    Retry tick: GitHub now reports the PR as ``MERGED`` with a merge
    commit; the PR's recorded ``headRefOid`` is still ``reviewed_sha``
    (the squashed tip). A correct keystone must RECONCILE this — consume
    the CLEAR and advance the FSM — not fail forever on the SHA precheck.
    """

    def __init__(self, *, reviewed_sha: str = _SHA) -> None:
        self.reviewed_sha = reviewed_sha
        self.merge_commit = "mergecommit0deadbeef"
        self.merged = False
        self.calls: list[list[str]] = []

    def _merge_state_payload(self) -> str:
        state = "MERGED" if self.merged else "OPEN"
        commit = self.merge_commit if self.merged else None
        return json.dumps({"state": state, "mergeCommit": {"oid": commit} if commit else None})

    def _do_merge(self) -> tuple[int, str, str]:
        if self.merged:
            # GitHub refuses to merge an already-merged PR.
            return (1, "", "Pull Request is not mergeable (405)")
        # The irreversible merge lands at GitHub.
        self.merged = True
        return (0, json.dumps({"sha": self.merge_commit}), "")

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        joined = " ".join(argv)
        if "headRefOid" in joined:
            # GitHub keeps reporting the squashed tip as headRefOid.
            return (0, self.reviewed_sha, "")
        if "isDraft" in joined:
            return (0, "false", "")
        if "statusCheckRollup" in joined:
            return (0, _GREEN, "")
        if "state,mergeCommit" in joined:
            return (0, self._merge_state_payload(), "")
        if "pulls" in joined and "merge" in joined:
            return self._do_merge()
        return (0, "", "")


class TestLostPostHookRecoverable(TestCase):
    """#928: a lost post-merge-hook must be RECOVERABLE, not a permanent brick.

    On current code the retry fails ``live_sha != reviewed_sha`` forever
    (the live head is now the merge commit / the PR is merged) and the
    loop never self-issues a replacement — a permanently stranded
    "merged-on-GitHub, not-in-FSM" ticket. After the fix the retry
    reconciles: it consumes the single-use CLEAR and advances the FSM.
    """

    def test_lost_post_hook_then_retry_reconciles_fsm(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        stub = _LostPostHookGhStub()

        # First attempt: the GitHub merge lands, but the post hook dies
        # before consuming the CLEAR (process kill / DB lock / rollback
        # between execute_bound_merge and record_merge_and_advance).
        boom = RuntimeError("post hook lost (process killed between execute and record)")
        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=stub),
            patch("teatree.core.merge.execution.record_merge_and_advance", side_effect=boom),
            pytest.raises(RuntimeError, match="post hook lost"),
        ):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

        # The merge IS on GitHub, but the FSM never advanced and the
        # CLEAR was never consumed.
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert stub.merged is True
        assert ticket.state == Ticket.State.IN_REVIEW
        assert clear.consumed_at is None
        assert not MergeAudit.objects.filter(clear=clear).exists()

        # Retry tick: a correct keystone reconciles instead of bricking.
        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=stub):
            outcome = merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert clear.consumed_at is not None
        audit = MergeAudit.objects.get(clear=clear)
        assert audit.merged_sha == stub.merge_commit
        # The reconciling retry must NOT issue a second irreversible
        # merge call — the PR is already merged.
        retry_merge_calls = [c for c in stub.calls if "pulls" in " ".join(c) and "merge" in " ".join(c)]
        assert len(retry_merge_calls) == 1
        assert outcome.merged_sha == stub.merge_commit

    def test_reconcile_only_when_merged_at_reviewed_sha(self) -> None:
        # Defence: a PR merged with a DIFFERENT head (force-push then a
        # third party merged a different tree) must NOT reconcile our
        # stale CLEAR — the SHA-bind guarantee still holds.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)

        def _merged_other_head(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "headRefOid" in joined:
                return (0, _MOVED, "")  # head moved off reviewed_sha
            if "isDraft" in joined:
                return (0, "false", "")
            if "statusCheckRollup" in joined:
                return (0, _GREEN, "")
            if "state,mergeCommit" in joined:
                return (0, '{"state": "MERGED", "mergeCommit": {"oid": "othermerge0"}}', "")
            return (0, "", "")

        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_merged_other_head),
            pytest.raises(MergePreconditionError, match="head moved"),
        ):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert clear.consumed_at is None

    def test_reconcile_falls_back_to_reviewed_sha_when_no_merge_commit(self) -> None:
        # GitHub reports the PR MERGED but exposes no mergeCommit oid
        # (rare API shape): reconciliation still completes, recording the
        # bound reviewed_sha as the merged sha.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)

        def _merged_no_commit(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "headRefOid" in joined:
                return (0, _SHA, "")
            if "isDraft" in joined:
                return (0, "false", "")
            if "statusCheckRollup" in joined:
                return (0, _GREEN, "")
            if "state,mergeCommit" in joined:
                return (0, '{"state": "MERGED", "mergeCommit": null}', "")
            return (0, "", "")

        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_merged_no_commit):
            outcome = merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert clear.consumed_at is not None
        assert outcome.merged_sha == _SHA

    def test_reconcile_consumes_clear_exactly_once(self) -> None:
        # Guarantee preserved: single-use survives the reconcile path. A
        # second reconcile tick on the now-consumed CLEAR is refused (the
        # CLEAR is no longer actionable) — no double audit, no replay.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        stub = _LostPostHookGhStub()
        stub.merged = True  # PR already merged by us (lost post-hook earlier)

        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=stub):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")
        clear.refresh_from_db()
        assert clear.consumed_at is not None
        assert MergeAudit.objects.filter(clear=clear).count() == 1

        # A redundant reconcile tick must not consume / audit again.
        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=stub),
            pytest.raises(MergePreconditionError, match="not actionable"),
        ):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")
        assert MergeAudit.objects.filter(clear=clear).count() == 1

    def test_substrate_clear_is_not_reconciled_without_human_approval(self) -> None:
        # Guarantee preserved: the substrate auto-merge refusal runs
        # BEFORE the §928 reconciliation, so a lost post-hook on a
        # substrate PR cannot be silently reconciled by the loop — it
        # still requires the recorded human approval.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket, blast_class=MergeClear.BlastClass.SUBSTRATE)
        stub = _LostPostHookGhStub()
        stub.merged = True

        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=stub),
            pytest.raises(MergePreconditionError, match="substrate"),
        ):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert clear.consumed_at is None

    def test_self_issued_clear_is_not_reconciled(self) -> None:
        # Guarantee preserved: maker≠checker runs before reconciliation —
        # a lost post-hook does not let a self-issued CLEAR slip through.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket, reviewer_identity="merge-loop")
        stub = _LostPostHookGhStub()
        stub.merged = True

        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=stub),
            pytest.raises(MergePreconditionError, match="independent"),
        ):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")
        clear.refresh_from_db()
        assert clear.consumed_at is None


class TestFetchPrMergeState(TestCase):
    """`fetch_pr_merge_state` fails closed so reconciliation never fires on bad data."""

    def test_gh_error_returns_empty_state(self) -> None:
        with patch(
            "teatree.backends.forge_merge_rpc.gh_runner",
            return_value=lambda *_a, **_k: (1, "", "api error"),
        ):
            state = ci_rollup.fetch_pr_merge_state("souliane/teatree", 1)
        assert state.state == ""
        assert state.is_merged is False

    def test_malformed_json_returns_empty_state(self) -> None:
        with patch(
            "teatree.backends.forge_merge_rpc.gh_runner",
            return_value=lambda *_a, **_k: (0, "{not json", ""),
        ):
            state = ci_rollup.fetch_pr_merge_state("souliane/teatree", 1)
        assert state.state == ""

    def test_non_dict_json_returns_empty_state(self) -> None:
        with patch(
            "teatree.backends.forge_merge_rpc.gh_runner",
            return_value=lambda *_a, **_k: (0, "[1, 2, 3]", ""),
        ):
            state = ci_rollup.fetch_pr_merge_state("souliane/teatree", 1)
        assert state.state == ""

    def test_merged_without_merge_commit_object(self) -> None:
        with patch(
            "teatree.backends.forge_merge_rpc.gh_runner",
            return_value=lambda *_a, **_k: (0, '{"state": "MERGED", "mergeCommit": null}', ""),
        ):
            state = ci_rollup.fetch_pr_merge_state("souliane/teatree", 1)
        assert state.is_merged is True
        assert state.merge_commit_oid == ""


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


_LOCKED = "database is locked"


class _LockOnce:
    """Wraps the real ``select_for_update`` manager call, raising a transient lock once.

    Models souliane/teatree#1520: a fix-agent holds the canonical-DB write
    lock for a moment while the loop's post hook opens its ``atomic()`` block,
    so the first attempt at the post-hook write hits ``OperationalError:
    database is locked``. After the simulated holder releases, the real
    manager call runs and the merge record lands.
    """

    def __init__(self, real_select_for_update: Callable[..., object]) -> None:
        self._real = real_select_for_update
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        if self.calls == 1:
            raise OperationalError(_LOCKED)
        return self._real(*args, **kwargs)


class TestMergeKeystoneTransientLockResilience(TestCase):
    """souliane/teatree#1520 — the post-hook write survives a transient lock.

    The merge keystone is the one path that advances a PR to MERGED. A bare
    ``OperationalError: database is locked`` from a concurrent canonical-DB
    writer must NOT abort the ceremony mid-flight; the bounded retry-on-locked
    around ``record_merge_and_advance``'s atomic write blocks-then-proceeds.
    """

    def test_transient_lock_in_post_hook_is_retried_not_crashed(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)

        lock_once = _LockOnce(MergeClear.objects.select_for_update)
        with (
            patch("teatree.core.db_retry.time.sleep"),
            patch.object(MergeClear.objects, "select_for_update", side_effect=lock_once),
        ):
            outcome = _run(clear, _GhStub())

        assert lock_once.calls >= 2, "the post-hook write was not retried past the transient lock"
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert clear.consumed_at is not None
        # Exactly one audit — the retry re-runs the idempotent post hook, it
        # does not double-merge.
        assert MergeAudit.objects.filter(clear=clear).count() == 1
        assert outcome.ticket_state == Ticket.State.MERGED

    def test_clear_issue_survives_a_transient_lock(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        request = ClearRequest(
            pr_id=4242,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            ticket=ticket,
        )

        real_create = MergeClear.objects.create
        call_count = {"n": 0}

        def _create_lock_once(*args: object, **kwargs: object) -> object:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OperationalError(_LOCKED)
            return real_create(*args, **kwargs)

        with (
            patch("teatree.core.db_retry.time.sleep"),
            patch.object(MergeClear.objects, "create", side_effect=_create_lock_once),
        ):
            clear = MergeClear.issue(request)

        assert call_count["n"] >= 2, "MergeClear.issue did not retry past the transient lock"
        assert clear.pk is not None
        assert MergeClear.objects.filter(pk=clear.pk).count() == 1


class TestIsTransientMergeResponse(TestCase):
    """The pure classifier separating a transient forge response from a refusal."""

    def test_zero_rc_is_never_transient(self) -> None:
        assert execution._is_transient_merge_response(0, '{"sha": "x"}', "") is False

    def test_empty_body_failure_is_transient(self) -> None:
        assert execution._is_transient_merge_response(1, "", "") is True

    def test_truncated_json_marker_is_transient(self) -> None:
        assert execution._is_transient_merge_response(1, "", "unexpected end of JSON input") is True

    def test_5xx_marker_is_transient(self) -> None:
        assert execution._is_transient_merge_response(1, "", "503 Service Unavailable") is True

    def test_policy_refusal_is_not_transient(self) -> None:
        assert execution._is_transient_merge_response(1, "", "Pull Request is not mergeable (405)") is False

    def test_policy_marker_wins_over_a_transient_token(self) -> None:
        # A refusal mentioning a transient-looking token is still a refusal.
        assert execution._is_transient_merge_response(1, "", "422 unprocessable; gateway timeout") is False

    def test_explicit_non_transient_message_is_not_transient(self) -> None:
        assert execution._is_transient_merge_response(1, "", "Validation failed: base must be a branch") is False


def _green_probe_response(joined: str, *, head: str = _SHA) -> tuple[int, str, str] | None:
    """The constant §17.4.3 GET-probe responses (head / draft / checks all healthy).

    Returns ``None`` when *joined* is not one of the three read-only probes, so
    a stub can fall through to its own merge / merge-state branches without
    repeating these three identical conditionals.
    """
    if "headRefOid" in joined:
        return (0, head, "")
    if "isDraft" in joined:
        return (0, "false", "")
    if "statusCheckRollup" in joined:
        return (0, _GREEN, "")
    return None


class _TransientThenSuccessGhStub:
    """A merge call that returns a truncated-JSON failure N times, then succeeds.

    Models the #1804 window: ``gh api PUT .../merge`` returns a non-zero exit
    with ``unexpected end of JSON input`` on a truncated/empty API response.
    The PR is NOT yet merged on those failing attempts (the merge did not
    land), so the merge-state probe keeps reporting OPEN. After
    ``fail_times`` transient failures the call succeeds and the merge lands.
    """

    def __init__(self, *, fail_times: int = 2, head: str = _SHA) -> None:
        self.fail_times = fail_times
        self.head = head
        self.merge_attempts = 0
        self.merged = False
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        joined = " ".join(argv)
        probe = _green_probe_response(joined, head=self.head)
        if probe is not None:
            return probe
        if "state,mergeCommit" in joined:
            state = "MERGED" if self.merged else "OPEN"
            commit = '{"oid": "merged0deadbeef"}' if self.merged else "null"
            return (0, f'{{"state": "{state}", "mergeCommit": {commit}}}', "")
        if "pulls" in joined and "merge" in joined:
            self.merge_attempts += 1
            if self.merge_attempts <= self.fail_times:
                return (1, "", "unexpected end of JSON input")
            self.merged = True
            return (0, '{"sha": "merged0deadbeef"}', "")
        return (0, "", "")


class TestTransientMergeRetry(TestCase):
    """#1813: a transient/empty-JSON forge merge response is retried, not stranded.

    The keystone merge step used to treat any non-head-moved ``gh api PUT
    .../merge`` failure as a fatal ``MergePreconditionError`` with no retry,
    so a truncated ``unexpected end of JSON input`` response left a fully
    cleared PR OPEN with its single-use CLEAR unconsumed. The retry must be
    guaranteed: classify a transient response distinctly, retry a bounded
    number of times, and never consume the CLEAR on a transient failure.
    """

    def test_truncated_json_merge_response_is_retried_then_succeeds(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        stub = _TransientThenSuccessGhStub(fail_times=2)

        with (
            patch("teatree.core.merge.execution.time.sleep"),
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=stub),
        ):
            outcome = merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

        assert stub.merge_attempts == 3, "the transient merge response was not retried to success"
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert clear.consumed_at is not None
        assert MergeAudit.objects.filter(clear=clear).count() == 1
        assert outcome.merged_sha == "merged0deadbeef"

    def test_transient_failure_until_exhausted_escalates_without_consuming_clear(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        # Never succeeds — every merge attempt is a truncated-JSON transient.
        stub = _TransientThenSuccessGhStub(fail_times=99)

        with (
            patch("teatree.core.merge.execution.time.sleep"),
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=stub),
            pytest.raises(MergeTransientError, match="transient"),
        ):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

        assert stub.merge_attempts >= 3, "the transient merge response was not retried before escalating"
        ticket.refresh_from_db()
        clear.refresh_from_db()
        # The CLEAR is idempotently reusable: a transient failure never
        # consumes it, so a manual / loop retry of the SAME CLEAR can merge.
        assert ticket.state == Ticket.State.IN_REVIEW
        assert clear.consumed_at is None
        assert not MergeAudit.objects.filter(clear=clear).exists()

    def test_policy_refusal_is_not_retried_and_escalates(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        attempts = {"merge": 0}

        def _policy_refusal(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "headRefOid" in joined:
                return (0, _SHA, "")
            if "isDraft" in joined:
                return (0, "false", "")
            if "statusCheckRollup" in joined:
                return (0, _GREEN, "")
            if "pulls" in joined and "merge" in joined:
                attempts["merge"] += 1
                return (1, "", "Pull Request is not mergeable (405)")
            return (0, "", "")

        with (
            patch("teatree.core.merge.execution.time.sleep") as sleep,
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_policy_refusal),
            pytest.raises(MergePreconditionError, match="failed"),
        ):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

        assert attempts["merge"] == 1, "a policy refusal must NOT be retried"
        assert not isinstance(sleep.call_args, tuple) or sleep.call_count == 0
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert clear.consumed_at is None

    def test_head_moved_is_not_retried(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        attempts = {"merge": 0}

        def _head_moved(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "headRefOid" in joined:
                return (0, _SHA, "")
            if "isDraft" in joined:
                return (0, "false", "")
            if "statusCheckRollup" in joined:
                return (0, _GREEN, "")
            if "pulls" in joined and "merge" in joined:
                attempts["merge"] += 1
                return (1, "", "Head branch was modified. Review and try the merge again. (409)")
            return (0, "", "")

        with (
            patch("teatree.core.merge.execution.time.sleep"),
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_head_moved),
            pytest.raises(MergeHeadMovedError),
        ):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

        assert attempts["merge"] == 1, "a head-moved refusal must NOT be retried"

    def test_transient_response_that_actually_landed_reconciles_no_second_merge(self) -> None:
        # The forge returned a truncated body but the merge DID land. The
        # retry's pre-attempt merge-state probe sees MERGED at reviewed_sha
        # and reconciles instead of re-issuing the (now 405-bricking) merge.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        state = {"merged": False, "merge_calls": 0}

        def _transient_but_landed(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            probe = _green_probe_response(joined)
            if probe is not None:
                return probe
            if "state,mergeCommit" in joined:
                if state["merged"]:
                    return (0, '{"state": "MERGED", "mergeCommit": {"oid": "landed0commit"}}', "")
                return (0, '{"state": "OPEN", "mergeCommit": null}', "")
            if "pulls" in joined and "merge" in joined:
                state["merge_calls"] += 1
                # The merge lands at GitHub but the response is truncated.
                state["merged"] = True
                return (1, "", "unexpected end of JSON input")
            return (0, "", "")

        with (
            patch("teatree.core.merge.execution.time.sleep"),
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_transient_but_landed),
        ):
            outcome = merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

        assert state["merge_calls"] == 1, "the merge must not be re-issued once it has landed"
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert clear.consumed_at is not None
        assert outcome.merged_sha == "landed0commit"

    def test_execute_bound_merge_classifies_empty_output_as_transient(self) -> None:
        # A bare empty response (rc != 0, no stderr marker) on the merge call
        # is transient — the truncated/empty-JSON class the #1804 window hit.
        attempts = {"merge": 0}

        def _empty_then_fail(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "state,mergeCommit" in joined:
                return (0, '{"state": "OPEN", "mergeCommit": null}', "")
            if "pulls" in joined and "merge" in joined:
                attempts["merge"] += 1
                return (1, "", "")
            return (0, "", "")

        with (
            patch("teatree.core.merge.execution.time.sleep"),
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_empty_then_fail),
            pytest.raises(MergeTransientError, match="transient"),
        ):
            execute_bound_merge(slug="souliane/teatree", pr_id=859, expected_head_oid=_SHA)

        assert attempts["merge"] >= 3, "an empty/truncated merge response was not retried"
