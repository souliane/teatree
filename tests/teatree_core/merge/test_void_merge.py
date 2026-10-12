from dataclasses import replace

import pytest
from django.test import TestCase

from teatree.core.forge_content import PrContentSnapshot
from teatree.core.merge.void_merge import VoidRefusal, VoidRefusedError, apply_void, check_operator, plan_void
from teatree.core.models import (
    CriticFinding,
    CriticVerdict,
    MergeAudit,
    MergeClear,
    MergeVoid,
    PullRequest,
    ReviewEvidence,
    ReviewVerdict,
    Rubric,
    RubricCriterion,
    Session,
    Ticket,
)
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.transition import TicketTransition
from teatree.core.session_identity import LOOP_RUNNER_SESSION_ID
from teatree.utils.agent_spawn_marker import dispatched_agent_env
from teatree.utils.env import patched_environ
from tests._loop_principal_env import pinned_loop_principal
from tests.factories import MergeAuditFactory, MergeClearFactory, ReviewVerdictFactory, merge_void, record_test_plan
from tests.void_merge_fixtures import (
    ABSENT,
    CONTENT_HEAD,
    ISSUE_URL,
    MERGE_BASE,
    MERGED_SHA,
    PR_URL,
    SLUG,
    TIP,
    Terminal,
    human_operator,
    landed_ticket,
    stub_forge,
    terminal,
)

State = Ticket.State
OTHER_PR_URL = f"https://github.com/{SLUG}/pull/42"
LEDGERS = (
    MergeClear,
    MergeAudit,
    ReviewVerdict,
    CriticVerdict,
    CriticFinding,
    Rubric,
    RubricCriterion,
    PlanArtifact,
    ReviewEvidence,
    PullRequest,
    Session,
    MergeVoid,
)


def _counts() -> dict[str, int]:
    return {model.__name__: model.objects.count() for model in (*LEDGERS, TicketTransition)}


def _void(**typed: Terminal) -> MergeVoid:
    plan = plan_void(PR_URL)
    return apply_void(plan, reason="history rewrite dropped the change", actor="alice", **(typed or terminal()))


class _VoidCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(human_operator())

    @staticmethod
    def landed(**kwargs: object) -> Ticket:
        return landed_ticket(**kwargs)


class TestVoidRestoresAndKeepsEveryLedger(_VoidCase):
    def setUp(self) -> None:
        super().setUp()
        self.ticket = self.landed()
        self.ticket.extra = {
            **self.ticket.extra,
            "anti_vacuity_attestations": {CONTENT_HEAD: {"result": "passed"}},
            "rubric_grades_by_head": {CONTENT_HEAD: [1]},
            "forge_merge_evidence": {"slug": SLUG, "pr_id": 41, "merged_sha": MERGED_SHA},
        }
        self.ticket.save()
        Session.objects.create(ticket=self.ticket, overlay="t3-teatree", visited_phases=["coding", "reviewing"])
        ReviewVerdictFactory(ticket=self.ticket, slug=SLUG, pr_id=41, reviewed_sha=CONTENT_HEAD)
        CriticVerdict.objects.create(ticket=self.ticket, transition="mark_delivered", grader_identity="critic-1")
        CriticFinding.objects.create(ticket=self.ticket, transition="mark_delivered", rubric_item="r", detail="d")
        Rubric.populate(self.ticket, ["the void keeps every ledger"])
        record_test_plan(self.ticket)
        ReviewEvidence.objects.create(
            ticket=self.ticket,
            kind=ReviewEvidence.Kind.COLD_REVIEW,
            reviewer_identity="cold-reviewer-41",
            verdict="pass",
            head_sha=CONTENT_HEAD,
            repos=[SLUG],
        )

    def test_the_ticket_returns_to_its_pre_merge_state_and_the_row_closes(self) -> None:
        with stub_forge():
            _void()

        self.ticket.refresh_from_db()
        assert self.ticket.state == State.CODED
        assert PullRequest.objects.get(url=PR_URL).state == PullRequest.State.CLOSED

    def test_nothing_is_deleted_and_exactly_one_transition_is_added(self) -> None:
        before, extra = _counts(), dict(self.ticket.extra)

        with stub_forge():
            _void()

        self.ticket.refresh_from_db()
        assert _counts() == {**before, "MergeVoid": 1, "TicketTransition": before["TicketTransition"] + 1}
        assert self.ticket.extra == extra
        assert Session.objects.get(ticket=self.ticket).visited_phases == ["coding", "reviewing"]
        newest = self.ticket.transitions.order_by("-created_at", "-pk").first()
        assert (newest.from_state, newest.to_state, newest.triggered_by) == (State.MERGED, State.CODED, "void_merge")

    def test_the_void_records_who_why_and_the_forge_evidence(self) -> None:
        with stub_forge():
            void = _void()

        row = PullRequest.objects.get(url=PR_URL)
        assert (void.actor, void.reason) == ("alice", "history rewrite dropped the change")
        assert void.voided_at is not None
        assert (void.host_kind, void.slug, void.pr_id, void.pr_url) == ("github", SLUG, 41, PR_URL)
        assert (void.merged_sha, void.content_head, void.merge_base) == (MERGED_SHA, CONTENT_HEAD, MERGE_BASE)
        assert (void.default_branch, void.default_tip) == ("main", TIP)
        assert void.changed_paths == ["src/a.py", "src/b.py"]
        assert void.evidence["tickets"] == [
            {"ticket_id": self.ticket.pk, "from_state": State.MERGED, "to_state": State.CODED}
        ]
        assert void.evidence["pull_requests"] == [
            {"pull_request_id": row.pk, "from_state": PullRequest.State.MERGED, "to_state": PullRequest.State.CLOSED}
        ]
        assert list(void.tickets.all()) == [self.ticket]


class TestVoidSources(_VoidCase):
    def test_a_ticket_past_merged_restores_from_its_edge_into_merged(self) -> None:
        ticket = self.landed(from_state=State.TESTED)
        ticket.retrospect()
        ticket.save()

        with stub_forge():
            _void()

        ticket.refresh_from_db()
        assert ticket.state == State.TESTED

    def test_a_ticket_keyed_on_the_pr_url_with_no_row_is_restored(self) -> None:
        owner = self.landed()
        keyed = self.landed(from_state=State.WORK_STARTED, issue_url=PR_URL, with_row=False)

        with stub_forge():
            void = _void()

        keyed.refresh_from_db()
        assert keyed.state == State.WORK_STARTED
        assert set(void.tickets.all()) == {owner, keyed}

    def test_a_ticket_named_only_by_its_recorded_forge_evidence_is_restored(self) -> None:
        self.landed()
        named = Ticket.objects.create(
            overlay="t3-teatree",
            state=State.MERGED,
            extra={"forge_merge_evidence": {"slug": SLUG.upper(), "pr_id": 41, "merged_sha": MERGED_SHA}},
        )
        TicketTransition.objects.create(ticket=named, from_state=State.PR_OPENED, to_state=State.MERGED)

        with stub_forge():
            _void()

        named.refresh_from_db()
        assert named.state == State.PR_OPENED

    def test_a_ticket_named_only_by_a_merge_audit_is_restored(self) -> None:
        ticket = self.landed(with_row=False)

        with stub_forge():
            _void()

        ticket.refresh_from_db()
        assert ticket.state == State.CODED

    def test_a_ticket_with_no_edge_into_merged_refuses_the_whole_void(self) -> None:
        restorable = self.landed()
        Ticket.objects.create(overlay="t3-teatree", state=State.MERGED, issue_url=PR_URL)
        before = _counts()

        with stub_forge(), pytest.raises(VoidRefusedError) as refusal:
            _void()

        assert refusal.value.code is VoidRefusal.NO_MERGE_EDGE
        assert _counts() == before
        restorable.refresh_from_db()
        assert restorable.state == State.MERGED


class TestContentRefusals(_VoidCase):
    def test_content_found_on_the_default_branch_refuses_without_a_write(self) -> None:
        self.landed()
        cases = {
            VoidRefusal.CONTENT_ON_DEFAULT: replace(ABSENT, tip_blobs={**ABSENT.tip_blobs, "src/b.py": "b1"}),
            VoidRefusal.CONTENT_CARRIED_BY_MERGE: replace(ABSENT, merge_blobs={**ABSENT.merge_blobs, "src/a.py": "a1"}),
            VoidRefusal.FORGE_UNREADABLE: PrContentSnapshot(unreadable="HTTP 403 on the tree read"),
            VoidRefusal.FORGE_NOT_MERGED: PrContentSnapshot(merged=False),
        }
        before = _counts()
        for code, snapshot in cases.items():
            with self.subTest(code=code), stub_forge(snapshot), pytest.raises(VoidRefusedError) as refusal:
                _void()
            assert refusal.value.code is code
        assert _counts() == before

    def test_a_forge_read_that_raises_refuses_as_unreadable(self) -> None:
        self.landed()

        with stub_forge(RuntimeError("connection reset")), pytest.raises(VoidRefusedError) as refusal:
            _void()

        assert refusal.value.code is VoidRefusal.FORGE_UNREADABLE
        assert "connection reset" in refusal.value.message

    def test_a_default_branch_that_moves_before_the_write_refuses(self) -> None:
        self.landed()
        before = _counts()

        with stub_forge(ABSENT, replace(ABSENT, default_tip="f" * 40)), pytest.raises(VoidRefusedError) as refusal:
            _void()

        assert refusal.value.code is VoidRefusal.PLAN_CHANGED
        assert _counts() == before

    def test_a_url_that_names_no_pull_request_refuses_before_any_read(self) -> None:
        with stub_forge() as probe, pytest.raises(VoidRefusedError) as refusal:
            plan_void(ISSUE_URL)

        assert refusal.value.code is VoidRefusal.NOT_A_PR_URL
        probe.assert_not_called()

    def test_a_merged_pr_with_nothing_recorded_refuses(self) -> None:
        with stub_forge(), pytest.raises(VoidRefusedError) as refusal:
            _void()

        assert refusal.value.code is VoidRefusal.NOTHING_TO_VOID


class TestOperatorRefusals(_VoidCase):
    def test_a_blank_reason_refuses(self) -> None:
        with pytest.raises(VoidRefusedError) as refusal:
            check_operator(reason="  ", actor="alice")

        assert refusal.value.code is VoidRefusal.REASON_MISSING

    def test_a_blank_or_agent_looking_actor_refuses(self) -> None:
        for actor in ("", "  ", "coding-agent", "cold-reviewer-41", "claude-orchestrator", "t3-loop", "critic-2"):
            with self.subTest(actor=actor), pytest.raises(VoidRefusedError) as refusal:
                check_operator(reason="history rewrite", actor=actor)
            assert refusal.value.code is VoidRefusal.ACTOR_NOT_HUMAN

    def test_a_named_human_passes(self) -> None:
        check_operator(reason="history rewrite", actor="alice")


class TestHumanOnlyGate(_VoidCase):
    def setUp(self) -> None:
        super().setUp()
        self.ticket = self.landed()
        self.before = _counts()

    def _refused(self, **typed: Terminal) -> VoidRefusal:
        with stub_forge(), pytest.raises(VoidRefusedError) as refusal:
            _void(**typed)
        assert _counts() == self.before
        self.ticket.refresh_from_db()
        assert self.ticket.state == State.MERGED
        return refusal.value.code

    def test_a_caller_with_no_terminal_is_refused(self) -> None:
        assert self._refused(**terminal(tty=False)) is VoidRefusal.NO_TERMINAL

    def test_a_human_who_does_not_type_yes_is_refused(self) -> None:
        assert self._refused(**terminal("no\n")) is VoidRefusal.DECLINED

    def test_the_loop_runner_is_refused_even_with_a_terminal_answering_yes(self) -> None:
        with pinned_loop_principal(LOOP_RUNNER_SESSION_ID, pid=4242):
            assert self._refused(**terminal()) is VoidRefusal.UNATTENDED

    def test_a_dispatched_agent_is_refused_even_with_a_terminal_answering_yes(self) -> None:
        with patched_environ(dispatched_agent_env()):
            assert self._refused(**terminal()) is VoidRefusal.AGENT_PROCESS

    def test_the_prompt_shows_the_evidence_before_the_human_answers(self) -> None:
        typed = terminal()

        with stub_forge():
            _void(**typed)

        prompt = typed["stdout"].getvalue()
        assert PR_URL in prompt
        assert "2 of 2 paths absent" in prompt
        assert f"ticket {self.ticket.pk}: merged -> coded" in prompt


class TestKeptIdempotentAndDryRun(_VoidCase):
    def test_a_ticket_still_evidenced_by_another_merged_pr_stays_merged(self) -> None:
        voided = self.landed()
        kept = self.landed(pr_url=OTHER_PR_URL, issue_url=f"https://github.com/{SLUG}/issues/8")
        MergeAuditFactory(clear=MergeClearFactory(ticket=kept, slug=SLUG, pr_id=41), merged_sha=MERGED_SHA)

        with stub_forge():
            plan = plan_void(PR_URL)
            void = apply_void(plan, reason="history rewrite", actor="alice", **terminal())

        kept.refresh_from_db()
        voided.refresh_from_db()
        assert kept.state == State.MERGED
        assert voided.state == State.CODED
        assert [entry["ticket_id"] for entry in void.evidence["kept"]] == [kept.pk]
        assert f"{SLUG}#42" in void.evidence["kept"][0]["reason"]

    def test_evidence_for_an_already_voided_pr_does_not_keep_a_ticket(self) -> None:
        ticket = self.landed(pr_url=OTHER_PR_URL)
        MergeAuditFactory(clear=MergeClearFactory(ticket=ticket, slug=SLUG, pr_id=41), merged_sha=MERGED_SHA)
        merge_void(slug=SLUG, pr_id=42)

        with stub_forge():
            _void()

        ticket.refresh_from_db()
        assert ticket.state == State.CODED

    def test_a_second_void_is_already_voided_and_reads_nothing(self) -> None:
        self.landed()
        with stub_forge():
            _void()
        after_first = _counts()

        with stub_forge() as probe, pytest.raises(VoidRefusedError) as refusal:
            _void()

        assert refusal.value.code is VoidRefusal.ALREADY_VOIDED
        probe.assert_not_called()
        assert _counts() == after_first

    def test_planning_alone_writes_nothing_and_needs_no_terminal(self) -> None:
        ticket = self.landed()
        before = _counts()

        with stub_forge():
            plan = plan_void(PR_URL)

        assert plan.refusal is None
        assert [restore["ticket_id"] for restore in plan.restores] == [ticket.pk]
        assert _counts() == before
