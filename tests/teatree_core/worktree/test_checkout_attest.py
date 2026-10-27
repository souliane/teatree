"""Checkout owner cards show the evidence that their approval binds."""

import re
from dataclasses import replace
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pytest
from django.db import OperationalError
from django.test import TestCase

from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.modelkit.owner_action import ActionRequest, action_marker
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.phases import ARCHITECTURAL_REVIEW_PHASE, KNOWN_PHASES
from teatree.core.modelkit.question_card import MAX_WORDS, QuestionCard, plain_text, word_count
from teatree.core.models import DeferredQuestion, PullRequest, Task
from teatree.core.owner_question_message import render_text
from teatree.core.worktree.checkout_attest import (
    AttestRefusedError,
    CheckoutAttestation,
    ask_owner_to_attest,
    attest_card,
    declined_attest_tasks,
    read_attestation,
)
from teatree.core.worktree.checkout_fence import writes_checkout
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
        assert f"it may hold up <{pull_request.url}|this pull request> while open" in text

    def test_folder_name_checkout_links_its_live_pull_request(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        pull_request = PullRequestFactory(ticket=ticket)
        task = TaskFactory(ticket=ticket, phase="coding")
        worktree = WorktreeFactory(ticket=ticket, repo_path="teatree")
        attestation = CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)

        row = ask_owner_to_attest(task, attestation)

        assert row is not None
        assert pull_request.url in render_text(row)

    def test_checkout_branch_pr_wins_over_older_live_pr(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        older = PullRequestFactory(ticket=ticket)
        selected = PullRequestFactory(ticket=ticket)
        task = TaskFactory(ticket=ticket)
        worktree = WorktreeFactory(ticket=ticket)
        ticket.extra = {"pr_url_by_branch": {worktree.branch: selected.url}}
        ticket.save(update_fields=["extra"])

        row = ask_owner_to_attest(
            task, CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
        )

        assert row is not None
        assert selected.url in render_text(row)
        assert older.url not in render_text(row)

    def test_oldest_live_pull_request_wins_without_a_branch_index(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        older = PullRequestFactory(ticket=ticket)
        newer = PullRequestFactory(ticket=ticket)

        _row, text = _ask(TaskFactory(ticket=ticket))

        assert older.url in text
        assert newer.url not in text

    def test_unsafe_pull_request_row_cannot_break_slack_link_markup(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        PullRequestFactory(ticket=ticket, url="https://github.com/souliane/teatree/pull/33|private>tail")

        _row, text = _ask(TaskFactory(ticket=ticket))

        assert "private>tail" not in text
        assert "no pull request for this folder was open when asked" in text

    def test_gitlab_pull_request_paths_remain_linked(self) -> None:
        for slug, path in (
            ("group/sub/repo", "-/merge_requests"),
            ("group/repo", "merge_requests"),
        ):
            with self.subTest(path=path):
                ticket = TicketFactory(short_description="Fix the login page")
                url = f"https://gitlab.example/{slug}/{path}/7"
                PullRequestFactory(ticket=ticket, repo=slug, url=url)
                task = TaskFactory(ticket=ticket)
                worktree = WorktreeFactory(ticket=ticket, repo_path=slug)

                row = ask_owner_to_attest(
                    task, CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
                )

                assert row is not None
                assert url in render_text(row)

    def test_folder_name_uses_its_branch_index_before_a_pr_row_exists(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        task = TaskFactory(ticket=ticket)
        worktree = WorktreeFactory(ticket=ticket, repo_path="teatree")
        url = "https://github.com/souliane/teatree/pull/33"
        ticket.extra = {"pr_url_by_branch": {worktree.branch: url}}
        ticket.save(update_fields=["extra"])

        row = ask_owner_to_attest(
            task, CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
        )

        assert row is not None
        assert url in render_text(row)

    def test_closed_indexed_branch_does_not_link_another_branch_pr(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        task = TaskFactory(ticket=ticket)
        worktree = WorktreeFactory(ticket=ticket)
        closed = PullRequestFactory(ticket=ticket, state=PullRequest.State.CLOSED)
        other = PullRequestFactory(ticket=ticket)
        ticket.extra = {"pr_url_by_branch": {worktree.branch: closed.url}}
        ticket.save(update_fields=["extra"])

        row = ask_owner_to_attest(
            task, CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
        )

        assert row is not None
        assert "no pull request for this folder was open when asked" in render_text(row)
        assert other.url not in render_text(row)

    def test_card_without_an_open_pull_request_says_it_blocks_none(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        PullRequestFactory(ticket=ticket, state=PullRequest.State.MERGED)
        PullRequestFactory(ticket=ticket, state=PullRequest.State.CLOSED)

        _row, text = _ask(TaskFactory(ticket=ticket))

        assert "no pull request for this folder was open when asked" in text
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
                    "nothing is running in its folder; it changed"
                    if host_evidence
                    else "the factory cannot rule out another editor"
                )
                assert doubt in text
                assert row.evidence["action"]["binding"] == "d" * 64

    def test_accept_and_decline_say_what_they_do_and_risk(self) -> None:
        accepts = {
            False: "All eligible waiting steps for this folder resume; "
            "edits the factory did not make, even unfinished ones, become part of their work.",
            True: "All eligible waiting steps for this folder resume; "
            "changes the factory did not make become part of their work.",
        }
        declines = {
            True: "The step stays stuck until the folder changes or the factory can check it safely; "
            "the pull request remains open.",
            False: "The step stays stuck until the folder changes or the factory can check it safely; "
            "no pull request waits.",
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
        shown = "Make the owner cards readable for someone who never saw the factory before..."
        cases = product(steps.items(), (False, True), (False, True))
        for number, ((phase, step), host_evidence, with_pull_request) in enumerate(cases, 10000):
            with self.subTest(phase=phase, host_evidence=host_evidence, with_pull_request=with_pull_request):
                issue_url = f"https://github.com/example/repo/issues/{number}"
                ticket = TicketFactory(issue_url=issue_url, short_description=title)
                if with_pull_request:
                    PullRequestFactory(ticket=ticket)
                task = TaskFactory(ticket=ticket, phase=phase)

                _row, text = _ask(task, host_evidence=host_evidence, commits=9_999_999, uncommitted=9_999_999)

                assert f"Should the {step} of <{issue_url}|{shown}>" in text
                assert len(_shown_lines(text)) <= 5

    def test_a_wordy_eighty_character_title_keeps_its_title_with_room_to_spare(self) -> None:
        title = "Workflow gap (dreaming Pass 2): When a PR body's design rationale rests on inven"
        for number, with_pull_request in enumerate((False, True), 30000):
            with self.subTest(with_pull_request=with_pull_request):
                ticket = TicketFactory(
                    issue_url=f"https://github.com/example/repo/issues/{number}", short_description=title
                )
                if with_pull_request:
                    PullRequestFactory(ticket=ticket)
                task = TaskFactory(ticket=ticket, phase="e2e")
                question, card = attest_card(
                    task,
                    CheckoutAttestation(
                        WorktreeFactory(ticket=ticket), CheckoutState("d" * 64, 99_999, 99_999), host_evidence=False
                    ),
                )

                assert card.problems(question) == []
                assert f"|{title.removesuffix(' inven')}...>" in question
                assert word_count(plain_text(card.view(question))) <= MAX_WORDS - 5

    def test_a_title_too_wordy_for_the_card_gives_way_to_this_ticket(self) -> None:
        title = "Fix a bug so a UI we use at HQ on a PC or a Mac is OK to go to in a day or so"
        assert len(title) <= 80
        ticket = TicketFactory(issue_url=ISSUE_URL, short_description=title)
        task = TaskFactory(ticket=ticket, phase="e2e")
        question, card = attest_card(
            task,
            CheckoutAttestation(
                WorktreeFactory(ticket=ticket), CheckoutState("d" * 64, 99_999, 99_999), host_evidence=False
            ),
        )

        assert card.problems(question) == []
        assert f"Should the end-to-end step of <{ISSUE_URL}|this ticket> run" in question

    def test_every_write_capable_phase_has_step_wording(self) -> None:
        assert ARCHITECTURAL_REVIEW_PHASE in KNOWN_PHASES
        for phase in KNOWN_PHASES:
            task = TaskFactory(phase=phase)
            if writes_checkout(task):
                worktree = WorktreeFactory(ticket=task.ticket)
                question, _card = attest_card(
                    task, CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
                )
                assert "Should the next step" not in question, phase

    def test_pull_request_lookup_failure_returns_no_card(self) -> None:
        task = TaskFactory()
        worktree = WorktreeFactory(ticket=task.ticket)
        attestation = CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)

        with patch("teatree.core.models.pull_request.PullRequestQuerySet.live", side_effect=OperationalError):
            row = ask_owner_to_attest(task, attestation)

        assert row is None
        assert not DeferredQuestion.objects.filter(parked_task=task).exists()

    def test_a_new_step_reuses_the_held_card_for_the_same_state(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        worktree = WorktreeFactory(ticket=ticket)
        attestation = CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
        task = TaskFactory(ticket=ticket, phase="coding")
        first = ask_owner_to_attest(task, attestation)
        assert first is not None

        task.phase = "debugging"
        task.save(update_fields=["phase"])
        second = ask_owner_to_attest(task, attestation)

        assert second is not None
        assert second.pk == first.pk
        first.refresh_from_db()
        assert first.is_pending
        assert "coding step" in second.question

    def test_two_waiting_steps_share_one_checkout_card(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        worktree = WorktreeFactory(ticket=ticket)
        attestation = CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
        coding = TaskFactory(ticket=ticket, phase="coding")
        retro = TaskFactory(ticket=ticket, phase="retro")

        first = ask_owner_to_attest(coding, attestation)
        second = ask_owner_to_attest(retro, attestation)
        repeated = ask_owner_to_attest(coding, attestation)

        assert first is not None
        assert second is not None
        assert repeated is not None
        assert repeated.pk == first.pk
        assert first.pk == second.pk
        assert DeferredQuestion.owner_pending().filter(evidence__action__target=str(worktree.pk)).count() == 1

    def test_mixed_steps_with_open_pull_request_keep_one_checkout_card(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        PullRequestFactory(ticket=ticket)
        worktree = WorktreeFactory(ticket=ticket)
        attestation = CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
        coding = TaskFactory(ticket=ticket, phase="coding")
        retro = TaskFactory(ticket=ticket, phase="retro")

        cards = [ask_owner_to_attest(task, attestation) for task in (coding, retro, coding, retro, coding)]

        assert all(card is not None for card in cards)
        assert len({card.pk for card in cards if card is not None}) == 1
        assert DeferredQuestion.objects.filter(evidence__action__target=str(worktree.pk)).count() == 1

    def test_old_task_specific_card_is_retired_when_shared_card_is_created(self) -> None:
        task = TaskFactory()
        worktree = WorktreeFactory(ticket=task.ticket)
        other = TaskFactory(ticket=task.ticket, phase="retro")
        attestation = CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
        question, card = attest_card(task, attestation)
        assert card.action is not None
        legacy_action = ActionRequest(**card.action, task_id=task.pk)
        legacy = DeferredQuestion.record(
            question,
            dedupe_marker=action_marker(legacy_action),
            parked_task=task,
            card=replace(card, action=legacy_action),
        )

        shared = ask_owner_to_attest(other, attestation)

        assert shared is not None
        legacy.refresh_from_db()
        assert not legacy.is_pending
        assert DeferredQuestion.owner_pending().filter(evidence__action__target=str(worktree.pk)).count() == 1

    def test_decline_on_shared_card_blocks_another_step_of_same_checkout_state(self) -> None:
        task = TaskFactory()
        worktree = WorktreeFactory(ticket=task.ticket)
        other = TaskFactory(ticket=task.ticket, phase="retro")
        state = CheckoutState("d" * 64, 1, 1)
        card = ask_owner_to_attest(task, CheckoutAttestation(worktree, state, host_evidence=False))
        assert card is not None
        card.apply_answer("Decline", resolved_via=DeferredQuestion.ResolvedVia.SLACK)

        assert declined_attest_tasks(worktree, state.digest, task_id=other.pk) == {other.pk}
        again = ask_owner_to_attest(other, CheckoutAttestation(worktree, state, host_evidence=False))
        assert again is None

        changed_host_evidence = ask_owner_to_attest(other, CheckoutAttestation(worktree, state, host_evidence=True))
        assert changed_host_evidence is None
        assert DeferredQuestion.objects.filter(evidence__action__target=str(worktree.pk)).count() == 1

    def test_changed_step_and_pull_request_link_replace_the_held_card(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        old_pr = PullRequestFactory(ticket=ticket)
        task = TaskFactory(ticket=ticket, phase="coding")
        worktree = WorktreeFactory(ticket=ticket)
        attestation = CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
        first = ask_owner_to_attest(task, attestation)
        assert first is not None

        old_pr.state = PullRequest.State.MERGED
        old_pr.save(update_fields=["state"])
        new_pr = PullRequestFactory(ticket=ticket)
        task.phase = "debugging"
        task.save(update_fields=["phase"])

        second = ask_owner_to_attest(task, attestation)

        assert second is not None
        assert second.pk != first.pk
        assert new_pr.url in render_text(second)
        first.refresh_from_db()
        assert not first.is_pending

    def test_a_changed_pull_request_state_replaces_the_held_card(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        pull_request = PullRequestFactory(ticket=ticket)
        task = TaskFactory(ticket=ticket)
        worktree = WorktreeFactory(ticket=ticket)
        attestation = CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
        first = ask_owner_to_attest(task, attestation)
        assert first is not None

        pull_request.state = PullRequest.State.MERGED
        pull_request.save(update_fields=["state"])
        second = ask_owner_to_attest(task, attestation)

        assert second is not None
        assert second.pk != first.pk
        first.refresh_from_db()
        assert not first.is_pending
        assert "no pull request for this folder was open when asked" in render_text(second)

    def test_changed_commit_count_names_a_changed_card_as_the_stale_reason(self) -> None:
        task = TaskFactory()
        worktree = WorktreeFactory(ticket=task.ticket)
        original = ask_owner_to_attest(
            task, CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
        )
        assert original is not None

        updated = ask_owner_to_attest(
            task, CheckoutAttestation(worktree, CheckoutState("d" * 64, 2, 1), host_evidence=False)
        )

        assert updated is not None
        assert updated.pk != original.pk
        original.refresh_from_db()
        assert original.dismissed_reason == "The checkout card changed since this question was asked"

    def test_corrupted_held_options_are_replaced_before_reasking(self) -> None:
        task = TaskFactory()
        worktree = WorktreeFactory(ticket=task.ticket)
        attestation = CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
        original = ask_owner_to_attest(task, attestation)
        assert original is not None
        original.options_json = "[]"
        original.save(update_fields=["options_json"])

        renewed = ask_owner_to_attest(task, attestation)

        assert renewed is not None
        assert renewed.pk != original.pk
        original.refresh_from_db()
        assert not original.is_pending
        assert "Accept" in render_text(renewed)

    def test_replacement_closes_the_posted_buttons(self) -> None:
        task = TaskFactory()
        worktree = WorktreeFactory(ticket=task.ticket)
        original = ask_owner_to_attest(
            task, CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
        )
        assert original is not None
        assert original.mark_mirrored(channel="D0DEMOOWNER", slack_ts="1800000000.000100")
        backend = MagicMock()

        with (
            patch("teatree.core.worktree.checkout_attest.resolve_owner_dm_backend", return_value=(backend, None)),
            self.captureOnCommitCallbacks(execute=True),
        ):
            ask_owner_to_attest(task, CheckoutAttestation(worktree, CheckoutState("e" * 64, 2, 1), host_evidence=False))

        backend.update_message.assert_called_once()
        assert "This question no longer needs an answer" in backend.update_message.call_args.kwargs["text"]
        assert "[Accept]" not in backend.update_message.call_args.kwargs["text"]

    def test_failed_card_closure_does_not_prevent_the_replacement(self) -> None:
        task = TaskFactory()
        worktree = WorktreeFactory(ticket=task.ticket)
        original = ask_owner_to_attest(
            task, CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
        )
        assert original is not None
        assert original.mark_mirrored(channel="D0DEMOOWNER", slack_ts="1800000000.000100")
        backend = MagicMock()

        with (
            patch("teatree.core.worktree.checkout_attest.resolve_owner_dm_backend", return_value=(backend, None)),
            patch("teatree.core.worktree.checkout_attest.replace_root", side_effect=OSError("Slack is unavailable")),
            self.captureOnCommitCallbacks(execute=True),
        ):
            replacement = ask_owner_to_attest(
                task, CheckoutAttestation(worktree, CheckoutState("e" * 64, 2, 1), host_evidence=False)
            )

        assert replacement is not None
        assert replacement.pk != original.pk
        assert replacement.is_pending

    def test_remote_lookup_failure_still_records_the_card(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        task = TaskFactory(ticket=ticket)
        worktree = WorktreeFactory(ticket=ticket, repo_path="teatree")

        with patch("teatree.core.worktree.checkout_attest.worktree_repo_slug", side_effect=OSError("remote failed")):
            row = ask_owner_to_attest(
                task, CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
            )

        assert row is not None
        assert row.is_pending

    def test_bare_folder_remote_lookup_is_time_bounded(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        task = TaskFactory(ticket=ticket)
        worktree = WorktreeFactory(ticket=ticket, repo_path="teatree", extra={"worktree_path": "/tmp/checkout"})

        with patch("teatree.core.worktree.branch_owner.run_with_status") as probe:
            probe.return_value.returncode = 0
            probe.return_value.stdout = "https://github.com/souliane/teatree.git"
            row = ask_owner_to_attest(
                task, CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
            )

        assert row is not None
        assert probe.call_args.kwargs["timeout"] == 5

    def test_new_digest_retires_old_cards_for_every_waiting_step(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        worktree = WorktreeFactory(ticket=ticket)
        coding = TaskFactory(ticket=ticket, phase="coding")
        retro = TaskFactory(ticket=ticket, phase="retro")
        original = CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
        old_coding = ask_owner_to_attest(coding, original)
        old_retro = ask_owner_to_attest(retro, original)
        assert old_coding is not None
        assert old_retro is not None

        renewed = ask_owner_to_attest(
            coding, CheckoutAttestation(worktree, CheckoutState("e" * 64, 2, 1), host_evidence=False)
        )

        assert renewed is not None
        old_coding.refresh_from_db()
        old_retro.refresh_from_db()
        assert not old_coding.is_pending
        assert not old_retro.is_pending

    def test_the_card_links_only_a_pull_request_in_its_checkout_repo(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        other = PullRequestFactory(ticket=ticket, repo="other/repo", url="https://github.com/other/repo/pull/999")
        own = PullRequestFactory(ticket=ticket, repo="souliane/teatree")

        _row, text = _ask(TaskFactory(ticket=ticket))

        assert own.url in text
        assert other.url not in text

    def test_pull_request_repo_case_does_not_hide_the_link(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        pull_request = PullRequestFactory(
            ticket=ticket, repo="Souliane/TeaTree", url="https://github.com/Souliane/TeaTree/pull/19"
        )
        task = TaskFactory(ticket=ticket)
        worktree = WorktreeFactory(ticket=ticket, repo_path="souliane/teatree")

        row = ask_owner_to_attest(
            task, CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
        )

        assert row is not None
        assert pull_request.url in render_text(row)

    def test_a_pull_request_in_another_repo_does_not_block_this_checkout(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        other = PullRequestFactory(ticket=ticket, repo="other/repo", url="https://github.com/other/repo/pull/999")

        _row, text = _ask(TaskFactory(ticket=ticket))

        assert "no pull request for this folder was open when asked" in text
        assert other.url not in text

    def test_design_review_and_wrap_up_do_not_claim_the_pull_request_waits(self) -> None:
        ticket = TicketFactory(short_description="Fix the login page")
        pull_request = PullRequestFactory(ticket=ticket)

        for phase in ("architectural_review", "retro"):
            with self.subTest(phase=phase):
                row, text = _ask(TaskFactory(ticket=ticket, phase=phase))

                assert pull_request.url in text
                assert "may hold up" not in text
                assert "may wait" not in text
                assert "also has" in render_text(row)

    def test_checked_facts_do_not_repeat_the_missing_process_evidence(self) -> None:
        row, _text = _ask(TaskFactory(), host_evidence=False)

        assert len(row.evidence["checked"]) == len(set(row.evidence["checked"]))

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
        worktree = WorktreeFactory(
            ticket=ticket, branch=f"{marker}-branch", repo_path="example/repo", extra={"worktree_path": f"/w/{marker}"}
        )
        state = CheckoutState("d" * 64, commits_ahead=1, uncommitted=1)
        for url in (
            f"https://github.com/example/repo/pull/5?private={marker}",
            f"https://github.com/example/repo/pull/5#{marker}",
            f"https://{marker}@github.com/example/repo/pull/5",
        ):
            with self.subTest(url=url):
                ticket.extra["pr_url_by_branch"] = {worktree.branch: url}
                ticket.save(update_fields=["extra"])
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

    def test_unlinked_eighty_character_seed_still_records_a_card(self) -> None:
        full = "The issue title is longer than the eighty character seed and should remain readable in cards"
        ticket = TicketFactory(issue_url="", short_description=full[:80], extra={"issue_title": full})

        row, text = _ask(TaskFactory(ticket=ticket))

        assert row.is_pending
        assert "step of The issue title is longer than the eighty character seed and should remain\u2026 run" in text

    def test_unlinked_title_with_terminal_punctuation_records_a_card(self) -> None:
        for ending in (".", "?", "!"):
            with self.subTest(ending=ending):
                ticket = TicketFactory(issue_url="", short_description=f"Make decisions clear{ending}")
                row, text = _ask(TaskFactory(ticket=ticket))

                assert row.is_pending
                assert "step of Make decisions clear run" in text

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
