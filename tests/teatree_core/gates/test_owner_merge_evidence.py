from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.backends.gitlab.merge_rpc import GitLabApiMergeRpc
from teatree.core.backend_protocols import PrMergeState
from teatree.core.gates.rubric_gate import (
    RubricNotSatisfiedError,
    RubricNotVerifiedError,
    check_rubric_satisfied,
    check_rubric_verified,
)
from teatree.core.models import ConfigSetting, PullRequest, Rubric, Ticket
from teatree.core.review.critic_rubric import completeness
from teatree.core.runners import RetroPhaseMarker
from tests.factories import MergeAuditFactory, merge_void

_SHA = "a" * 40
_PR_STATE = "teatree.core.merge.ci_rollup.CodeHostQuery.pr_merge_state"


def _ticket(*, merger: str = "", sha: str = _SHA) -> Ticket:
    ticket = Ticket.objects.create(overlay="test", state=Ticket.State.RETRO_RECORDED)
    ticket.merge_extra(
        set_keys={"forge_merge_evidence": {"slug": "example/repo", "pr_id": ticket.pk, "merged_sha": sha}}
    )
    if merger:
        ticket.merge_extra(merge_into_dicts={"forge_merge_evidence": {"merged_by": merger}})
    PullRequest.objects.create(
        ticket=ticket,
        url=f"https://github.com/example/repo/pull/{ticket.pk}",
        repo="example/repo",
        iid=str(ticket.pk),
        state=PullRequest.State.MERGED,
    )
    return ticket


class TestRecordForgeMerger(TestCase):
    def test_gitlab_head_sha_alone_cannot_record_owner_merger(self) -> None:
        ConfigSetting.objects.set_value("user_identity_aliases", ["alice"])
        cases = (
            ({"merge_commit_sha": _SHA, "sha": "b" * 40}, True),
            ({"squash_commit_sha": _SHA, "sha": "b" * 40}, True),
            ({"sha": _SHA}, False),
            ({"merge_commit_sha": "b" * 40, "squash_commit_sha": "c" * 40, "sha": _SHA}, False),
        )
        for fields, should_record in cases:
            with self.subTest(fields=fields):
                ticket = _ticket()
                Rubric.populate(ticket, ["ungraded criterion"])
                payload = {"state": "merged", "merge_user": {"username": "alice"}, **fields}
                state = GitLabApiMergeRpc(
                    SimpleNamespace(get_json=lambda _endpoint, payload=payload: payload)
                ).fetch_pr_merge_state(slug="example/repo", pr_id=ticket.pk)
                with patch(_PR_STATE, return_value=state):
                    RetroPhaseMarker(ticket).run()
                ticket.refresh_from_db()
                assert (ticket.extra["forge_merge_evidence"].get("merged_by") == "alice") is should_record
                if should_record:
                    assert check_rubric_verified(ticket) is None
                else:
                    with pytest.raises(RubricNotVerifiedError, match="ungraded"):
                        check_rubric_verified(ticket)

    def test_records_merger_only_for_the_recorded_merged_sha(self) -> None:
        ticket = _ticket()
        state = PrMergeState(
            state="MERGED", merge_commit_oid=_SHA.upper(), raw_merge_commit_oid=_SHA.upper(), merged_by=" Alice "
        )
        with patch(_PR_STATE, return_value=state) as read:
            RetroPhaseMarker(ticket).run()
        read.assert_called_once_with()
        ticket.refresh_from_db()
        assert ticket.extra["retro_scheduled"] is True
        assert ticket.extra["forge_merge_evidence"] == {
            "slug": "example/repo",
            "pr_id": ticket.pk,
            "merged_sha": _SHA,
            "merged_by": "Alice",
        }

    def test_squash_sha_can_match_when_merge_sha_differs(self) -> None:
        ticket = _ticket()
        state = PrMergeState(
            state="MERGED",
            merge_commit_oid="b" * 40,
            raw_merge_commit_oid="b" * 40,
            squash_commit_oid=_SHA.upper(),
            merged_by="alice",
        )
        with patch(_PR_STATE, return_value=state):
            RetroPhaseMarker(ticket).run()
        ticket.refresh_from_db()
        assert ticket.extra["forge_merge_evidence"]["merged_by"] == "alice"

    def test_open_mismatched_or_blank_merger_records_nothing(self) -> None:
        states = (
            PrMergeState(state="OPEN", merge_commit_oid=_SHA, merged_by="alice"),
            PrMergeState(state="MERGED", merge_commit_oid="b" * 40, merged_by="alice"),
            PrMergeState(state="MERGED", merge_commit_oid=_SHA, merged_by=" "),
        )
        for state in states:
            with self.subTest(state=state):
                ticket = _ticket()
                with patch(_PR_STATE, return_value=state):
                    RetroPhaseMarker(ticket).run()
                ticket.refresh_from_db()
                assert "merged_by" not in ticket.extra["forge_merge_evidence"]
                assert ticket.extra["retro_scheduled"] is True

    def test_failed_read_is_inconclusive_and_later_run_retries(self) -> None:
        ticket = _ticket()
        with patch(_PR_STATE, side_effect=RuntimeError("forge unavailable")):
            RetroPhaseMarker(ticket).run()
        ticket.refresh_from_db()
        assert "merged_by" not in ticket.extra["forge_merge_evidence"]
        with patch(
            _PR_STATE,
            return_value=PrMergeState(
                state="MERGED", merge_commit_oid=_SHA, raw_merge_commit_oid=_SHA, merged_by="alice"
            ),
        ):
            RetroPhaseMarker(ticket).run()
        ticket.refresh_from_db()
        assert ticket.extra["forge_merge_evidence"]["merged_by"] == "alice"

    def test_malformed_merge_state_records_nothing(self) -> None:
        ticket = _ticket()
        malformed = SimpleNamespace(is_merged=True, merge_commit_oid=None, squash_commit_oid=None, merged_by="alice")
        with patch(_PR_STATE, return_value=malformed):
            RetroPhaseMarker(ticket).run()
        ticket.refresh_from_db()
        assert "merged_by" not in ticket.extra["forge_merge_evidence"]

    def test_audit_existing_merger_and_malformed_sha_skip_the_read(self) -> None:
        audited = _ticket()
        MergeAuditFactory(clear__ticket=audited)
        already_recorded = _ticket(merger="alice")
        malformed = _ticket(sha="invalid")
        missing = Ticket.objects.create(overlay="test", state=Ticket.State.RETRO_RECORDED)
        voided = _ticket()
        merge_void(tickets=[voided], slug="example/repo", pr_id=voided.pk)
        with patch(_PR_STATE) as read:
            for ticket in (audited, already_recorded, malformed, missing, voided):
                RetroPhaseMarker(ticket).run()
        read.assert_not_called()


class TestOwnerMergeRubricWaiver(TestCase):
    def setUp(self) -> None:
        ConfigSetting.objects.set_value("user_identity_aliases", ["alice"])

    def test_missing_forge_evidence_keeps_the_ordinary_refusal(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.RETRO_RECORDED)
        Rubric.populate(ticket, ["ungraded criterion"])
        with pytest.raises(RubricNotVerifiedError, match="ungraded"):
            check_rubric_verified(ticket)

    def test_owner_merger_waives_ungraded_and_missing_rubrics(self) -> None:
        with_rubric = _ticket(merger=" Alice ")
        Rubric.populate(with_rubric, ["first criterion", "second criterion"])
        without_rubric = _ticket(merger="alice")
        with self.assertLogs("teatree.core.gates.rubric_gate", level="INFO") as logs:
            assert check_rubric_verified(with_rubric) is None
            assert check_rubric_verified(without_rubric) is None
        assert len(logs.output) == 2
        assert all(
            str(ticket.pk) in line for ticket, line in zip((with_rubric, without_rubric), logs.output, strict=True)
        )
        assert completeness(with_rubric) is None

    def test_recorded_fail_still_blocks_and_merge_time_stays_strict(self) -> None:
        ticket = _ticket(merger="alice")
        rubric = Rubric.populate(ticket, ["failed criterion", "ungraded criterion"])
        rubric.criteria.get(ordinal=0).record_grade(status="fail", grader_identity="cold-reviewer", reviewed_sha=_SHA)
        with pytest.raises(RubricNotVerifiedError, match="FAIL"):
            check_rubric_verified(ticket)
        with pytest.raises(RubricNotSatisfiedError, match="ungraded"):
            check_rubric_satisfied(ticket, _SHA, transition="merge")

    def test_nonowner_empty_alias_audit_and_live_pr_still_block(self) -> None:
        cases = ("nonowner", "empty_aliases", "blank_alias", "audit", "live_pr", "voided")
        for case in cases:
            with self.subTest(case=case):
                ticket = _ticket(merger="bob" if case == "nonowner" else " " if case == "blank_alias" else "alice")
                Rubric.populate(ticket, ["ungraded criterion"])
                if case == "empty_aliases":
                    ConfigSetting.objects.set_value("user_identity_aliases", [])
                if case == "blank_alias":
                    ConfigSetting.objects.set_value("user_identity_aliases", [" "])
                if case == "audit":
                    MergeAuditFactory(clear__ticket=ticket)
                if case == "voided":
                    merge_void(tickets=[ticket], slug="example/repo", pr_id=ticket.pk)
                if case == "live_pr":
                    sibling_id = ticket.pk + 10_000
                    PullRequest.objects.create(
                        ticket=ticket,
                        url=f"https://github.com/example/repo/pull/{sibling_id}",
                        repo="example/repo",
                        iid=str(sibling_id),
                    )
                with pytest.raises(RubricNotVerifiedError, match="ungraded"):
                    check_rubric_verified(ticket)
                ConfigSetting.objects.set_value("user_identity_aliases", ["alice"])

    def test_closed_sibling_does_not_block_owner_delivery(self) -> None:
        ticket = _ticket(merger="alice")
        Rubric.populate(ticket, ["ungraded criterion"])
        sibling_id = ticket.pk + 10_000
        PullRequest.objects.create(
            ticket=ticket,
            url=f"https://github.com/example/repo/pull/{sibling_id}",
            repo="example/repo",
            iid=str(sibling_id),
            state=PullRequest.State.CLOSED,
        )
        assert check_rubric_verified(ticket) is None
