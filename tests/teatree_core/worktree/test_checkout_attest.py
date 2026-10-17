"""Checkout owner cards show the evidence that their approval binds."""

import re
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from django.db import OperationalError
from django.test import TestCase

from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import QuestionCard
from teatree.core.models import DeferredQuestion, PullRequest, Task
from teatree.core.owner_question_message import render_text
from teatree.core.worktree.checkout_attest import (
    AttestRefusedError,
    CheckoutAttestation,
    ask_owner_to_attest,
    attest_card,
    read_attestation,
)
from teatree.core.worktree.checkout_state import CheckoutState, checkout_state
from teatree.core.worktree.occupancy import acquire
from tests.factories import PullRequestFactory, TaskFactory, TicketFactory, WorktreeFactory
from tests.teatree_core.models._shared import _init_repo_with_branch

ISSUE_URL = "https://github.com/example/repo/issues/17"
LONG_TITLE = "Make the owner cards say what the task does, what it blocks and what each option does for a reader"
TRIMMED_TITLE = "Make the owner cards say what the task does, what it blocks and what each..."


def _ask(
    task: Task, *, host_evidence: bool = False, commits: int = 3, uncommitted: int = 2
) -> tuple[DeferredQuestion, str]:
    worktree = WorktreeFactory(ticket=task.ticket)
    state = CheckoutState("d" * 64, commits_ahead=commits, uncommitted=uncommitted)
    row = ask_owner_to_attest(task, CheckoutAttestation(worktree, state, host_evidence=host_evidence))
    assert row is not None
    return row, render_text(row)


def _shown_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip() and not re.fullmatch(r"ref: question \d+", line)]


class TestCheckoutAttestCard(TestCase):
    def test_card_names_the_step_and_links_its_pull_request(self) -> None:
        ticket = TicketFactory(issue_url=ISSUE_URL, short_description="Fix the login page")
        pull_request = PullRequestFactory(ticket=ticket)

        _row, text = _ask(TaskFactory(ticket=ticket, phase="coding"))

        assert f"Should the coding step of <{ISSUE_URL}|Fix the login page>" in text
        assert "It will write the change;" in text
        assert f"<{pull_request.url}|its pull request> waits on it" in text

    def test_card_without_an_open_pull_request_says_it_blocks_none(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        PullRequestFactory(ticket=ticket, state=PullRequest.State.MERGED)
        PullRequestFactory(ticket=ticket, state=PullRequest.State.CLOSED)

        _row, text = _ask(TaskFactory(ticket=ticket))

        assert "it blocks no pull request" in text
        assert "/pull/" not in text

    def test_card_never_uses_internal_words(self) -> None:
        for host_evidence, with_pull_request in product((False, True), repeat=2):
            with self.subTest(host_evidence=host_evidence, with_pull_request=with_pull_request):
                ticket = TicketFactory(short_description="Fix the login page")
                if with_pull_request:
                    PullRequestFactory(ticket=ticket)

                row, text = _ask(TaskFactory(ticket=ticket), host_evidence=host_evidence)

                folded = text.casefold()
                assert not [word for word in ("checkout", "host process list", "live process") if word in folded]
                assert "3 commits and 2 uncommitted files" in text
                doubt = (
                    "nothing is running in its folder, but the folder changed since its last run"
                    if host_evidence
                    else "the factory cannot prove no program is still editing it"
                )
                assert doubt in text
                assert row.evidence["action"]["binding"] == "d" * 64

    def test_accept_and_decline_say_what_they_do_and_risk(self) -> None:
        accepts = {
            False: "The step runs once on the folder as it is; "
            "edits the factory did not make, even unfinished ones, become part of its work.",
            True: "The step resumes on the folder as it is; changes the factory did not make become part of its work.",
        }
        declines = {
            True: "The step stays stuck until the folder changes, and its pull request keeps waiting.",
            False: "The step stays stuck until the folder changes; no pull request waits on it.",
        }
        for host_evidence, with_pull_request in product((False, True), repeat=2):
            with self.subTest(host_evidence=host_evidence, with_pull_request=with_pull_request):
                ticket = TicketFactory(short_description="Fix the login page")
                if with_pull_request:
                    PullRequestFactory(ticket=ticket)

                row, text = _ask(TaskFactory(ticket=ticket), host_evidence=host_evidence)

                assert f"[Accept] (recommended) - {accepts[host_evidence]}" in text
                assert f"[Decline] - {declines[with_pull_request]}" in text
                card = QuestionCard.from_row(row.evidence, row.options_json)
                assert card is not None
                assert [(option.label, option.recommended) for option in card.options] == [
                    ("Accept", True),
                    ("Decline", False),
                ]
                assert card.decision == OwnerDecision.IRREVERSIBLE
                assert card.action is not None
                assert card.action["binding"] == "d" * 64

    def test_every_writing_step_fits_the_limits_in_five_lines(self) -> None:
        steps = {
            "coding": "coding step",
            "code": "coding step",
            "testing": "testing step",
            "test": "testing step",
            "shipping": "shipping step",
            "ship": "shipping step",
            "debugging": "debugging step",
            "e2e": "end-to-end step",
            "retro": "wrap-up step",
            "retrospect": "wrap-up step",
            "architectural_review": "design review step",
            "zorblax_phase": "next step",
        }
        title = "Make the owner cards readable for someone who never saw the factory before today"
        cases = product(steps.items(), (False, True), (False, True))
        for number, ((phase, step), host_evidence, with_pull_request) in enumerate(cases, 10000):
            with self.subTest(phase=phase, host_evidence=host_evidence, with_pull_request=with_pull_request):
                issue_url = f"https://github.com/example/repo/issues/{number}"
                ticket = TicketFactory(issue_url=issue_url, short_description=title)
                if with_pull_request:
                    PullRequestFactory(ticket=ticket)
                task = TaskFactory(ticket=ticket, phase=phase)

                _row, text = _ask(task, host_evidence=host_evidence, commits=99999, uncommitted=99999)

                assert f"Should the {step} of <{issue_url}|{title}>" in text
                worktree = WorktreeFactory(ticket=ticket)
                state = CheckoutState("e" * 64, commits_ahead=99999, uncommitted=99999)
                question, card = attest_card(task, CheckoutAttestation(worktree, state, host_evidence=host_evidence))
                assert card.problems(question) == []
                assert len(_shown_lines(text)) <= 5

    def test_private_ticket_data_never_reaches_the_card(self) -> None:
        marker = "zqxprivate"
        ticket = TicketFactory(
            issue_url=ISSUE_URL,
            short_description="Fix the login page",
            context=f"secret {marker} context",
            extra={"issue_title": "Fix the login page", "labels": [marker], "notes": f"note {marker}"},
            overlay=f"overlay-{marker}",
            variant=marker,
            repos=[f"{marker}/repo"],
            repo_namespaced_key=f"{marker}:17",
        )
        PullRequestFactory(ticket=ticket, url="https://github.com/example/repo/pull/5", repo=f"{marker}/repo")
        task = TaskFactory(
            ticket=ticket, subject=f"Do {marker}", execution_reason=f"because {marker}", phase=f"{marker}_phase"
        )
        worktree = WorktreeFactory(ticket=ticket, branch=f"{marker}-branch", extra={"worktree_path": f"/w/{marker}"})
        state = CheckoutState("d" * 64, commits_ahead=1, uncommitted=1)

        row = ask_owner_to_attest(task, CheckoutAttestation(worktree, state, host_evidence=False))

        assert row is not None
        text = render_text(row)
        assert "next step" in text
        assert marker not in text

    def test_a_cut_short_description_gives_way_to_the_whole_issue_title(self) -> None:
        cuts = (LONG_TITLE[:80], "Make the owner cards say what the task does\u2026", "Make the owner cards say...")
        for number, cut in enumerate(cuts, 20000):
            with self.subTest(cut=cut):
                ticket = TicketFactory(
                    issue_url=f"https://github.com/example/repo/issues/{number}",
                    short_description=cut,
                    extra={"issue_title": LONG_TITLE},
                )

                _row, text = _ask(TaskFactory(ticket=ticket))

                assert f"|{TRIMMED_TITLE}>" in text

    def test_a_whole_short_description_still_wins_over_the_issue_title(self) -> None:
        ticket = TicketFactory(
            issue_url=ISSUE_URL, short_description="Readable owner cards", extra={"issue_title": LONG_TITLE}
        )

        _row, text = _ask(TaskFactory(ticket=ticket))

        assert f"<{ISSUE_URL}|Readable owner cards>" in text

    def test_long_legacy_title_keeps_the_card_answerable(self) -> None:
        ticket = TicketFactory(
            issue_url="https://github.com/example/repo/issues/17",
            short_description="",
            extra={"issue_title": "A checkout title that is much too long " * 8},
        )
        task = TaskFactory(ticket=ticket)
        worktree = WorktreeFactory(ticket=ticket)
        attestation = CheckoutAttestation(
            worktree, CheckoutState("d" * 64, commits_ahead=1, uncommitted=2), host_evidence=True
        )

        question, card = attest_card(task, attestation)

        assert card.problems(question) == []
        assert "A checkout title" in question

    def test_non_text_legacy_title_uses_a_safe_fallback(self) -> None:
        ticket = TicketFactory(short_description="", extra={"issue_title": {"private": "data"}})
        task = TaskFactory(ticket=ticket)
        worktree = WorktreeFactory(ticket=ticket)
        attestation = CheckoutAttestation(worktree, CheckoutState("d" * 64, 0, 0), host_evidence=True)

        question, _card = attest_card(task, attestation)

        assert "this ticket" in question
        assert "private" not in question

    def test_unavailable_question_store_returns_to_the_park_notice_path(self) -> None:
        task = TaskFactory(ticket=TicketFactory())
        worktree = WorktreeFactory(ticket=task.ticket)
        attestation = CheckoutAttestation(worktree, CheckoutState("d" * 64, 0, 0), host_evidence=True)

        with patch("teatree.core.worktree.checkout_attest.DeferredQuestion.record", side_effect=OperationalError("db")):
            assert ask_owner_to_attest(task, attestation) is None

    def test_changed_host_evidence_supersedes_the_old_pending_card(self) -> None:
        task = TaskFactory(ticket=TicketFactory())
        worktree = WorktreeFactory(ticket=task.ticket)
        state = CheckoutState("d" * 64, 0, 1)
        old = ask_owner_to_attest(task, CheckoutAttestation(worktree, state, host_evidence=True))

        renewed = ask_owner_to_attest(task, CheckoutAttestation(worktree, state, host_evidence=False))

        assert old is not None
        assert renewed is not None
        old.refresh_from_db()
        assert old.dismissed_at is not None
        assert renewed.pk != old.pk
        assert renewed.evidence["action"]["host_evidence"] is False
        assert DeferredQuestion.owner_pending().filter(evidence__action__target=str(worktree.pk)).count() == 1


class TestReadAttestation(TestCase):
    def setUp(self) -> None:
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.checkout = Path(temporary.name) / "checkout"
        _init_repo_with_branch(self.checkout, branch="feature", commits_ahead=0)
        self.worktree = WorktreeFactory(extra={"worktree_path": str(self.checkout)})

    def _read(self, table: ProcessTable, *, expected_digest: str = "") -> CheckoutAttestation:
        with patch("teatree.core.worktree.checkout_attest.read_process_table", return_value=table):
            return read_attestation(self.worktree, expected_digest=expected_digest)

    def test_a_busy_or_occupied_checkout_is_worth_retrying(self) -> None:
        with pytest.raises(AttestRefusedError, match="live or unverifiable processes") as busy:
            self._read(ProcessTable(frozenset({self.checkout}), "/proc"))
        acquire(self.worktree, holder="task:7", holder_session="session-A")
        with pytest.raises(AttestRefusedError, match="unoccupied") as occupied:
            self._read(ProcessTable(frozenset(), "/proc"))

        assert busy.value.retryable
        assert occupied.value.retryable

    def test_a_checkout_that_moved_since_the_question_is_final(self) -> None:
        with pytest.raises(AttestRefusedError, match="changed since the owner was asked") as moved:
            self._read(ProcessTable(frozenset(), "/proc"), expected_digest="0" * 64)

        assert not moved.value.retryable

    def test_a_matching_checkout_reads_without_writing(self) -> None:
        digest = checkout_state(self.checkout).digest

        attestation = self._read(ProcessTable(frozenset(), "/proc"), expected_digest=digest)

        assert attestation.recorded == digest
        self.worktree.refresh_from_db()
        assert "attested_state" not in self.worktree.get_extra()

    def test_unreadable_same_user_process_does_not_count_as_host_evidence(self) -> None:
        table = ProcessTable(frozenset(), "/host-proc", (), ("pid 7 unreadable",))
        attestation = self._read(table)

        assert not attestation.host_evidence
        assert attestation.recorded.startswith("owner:")
        with (
            patch("teatree.core.worktree.checkout_attest.read_process_table", return_value=table),
            pytest.raises(AttestRefusedError, match="cannot rule out live processes") as error,
        ):
            read_attestation(self.worktree, requested_host_evidence=True)
        assert error.value.retryable

    def test_unreadable_git_state_is_retryable(self) -> None:
        with (
            patch("teatree.core.worktree.checkout_attest.checkout_state", side_effect=OSError("transient")),
            pytest.raises(AttestRefusedError, match="state could not be read") as error,
        ):
            self._read(ProcessTable(frozenset(), "/proc"))

        assert error.value.retryable
