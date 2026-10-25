"""Owner cards for waits that arise outside a parked agent task."""

from django.test import TestCase

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import QuestionCard, plain_text
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.review.owner_waits import ask_owner_to_approve_post, ask_owner_to_read, settle_live_post_wait

PR_URL = "https://github.com/example/repo/pull/12"


class TestOwnerWaitCards(TestCase):
    def test_held_pr_card_is_idempotent_per_head_and_replaces_old_head(self) -> None:
        first = ask_owner_to_read(PR_URL, "A reviewed change", "a" * 40)

        assert first is not None
        assert first.evidence["decision"] == OwnerDecision.ARCHITECTURE
        assert first.evidence["subject"] == PR_URL
        assert ask_owner_to_read(PR_URL, "A reviewed change", "a" * 40).pk == first.pk
        assert DeferredQuestion.owner_pending().count() == 1

        second = ask_owner_to_read(PR_URL, "A reviewed change", "b" * 40)

        first.refresh_from_db()
        assert first.status == DeferredQuestion.STATUS_DISMISSED
        assert second is not None
        assert second.pk != first.pk
        assert list(DeferredQuestion.owner_pending().values_list("pk", flat=True)) == [second.pk]

    def test_held_pr_card_reopens_when_head_returns(self) -> None:
        first = ask_owner_to_read(PR_URL, "A reviewed change", "a" * 40)
        second = ask_owner_to_read(PR_URL, "A reviewed change", "b" * 40)

        returned = ask_owner_to_read(PR_URL, "A reviewed change", "a" * 40)

        assert first is not None
        assert second is not None
        assert returned is not None
        assert returned.pk not in {first.pk, second.pk}
        assert list(DeferredQuestion.owner_pending().values_list("pk", flat=True)) == [returned.pk]

    def test_dismissed_same_head_held_pr_card_reopens(self) -> None:
        first = ask_owner_to_read(PR_URL, "A reviewed change", "a" * 40)
        assert first is not None
        first.mark_stale("age ladder expired", resolver_id="test")

        reopened = ask_owner_to_read(PR_URL, "A reviewed change", "a" * 40)

        assert reopened is not None
        assert reopened.pk != first.pk
        assert list(DeferredQuestion.owner_pending().values_list("pk", flat=True)) == [reopened.pk]

    def test_refused_live_post_has_one_owner_card_for_the_review(self) -> None:
        first = ask_owner_to_approve_post(PR_URL)

        assert first is not None
        assert first.evidence["decision"] == OwnerDecision.PUBLIC_POST
        assert first.evidence["subject"] == PR_URL
        assert ask_owner_to_approve_post(PR_URL).pk == first.pk
        assert DeferredQuestion.owner_pending().count() == 1

    def test_cards_name_the_separate_action_each_answer_requires(self) -> None:
        held = ask_owner_to_read(PR_URL, "A reviewed change", "a" * 40)
        post = ask_owner_to_approve_post("https://github.com/example/repo/pull/13")

        assert held is not None
        assert post is not None
        held_card = QuestionCard.from_row(held.evidence, held.options_json)
        post_card = QuestionCard.from_row(post.evidence, post.options_json)
        assert held_card is not None
        assert post_card is not None
        assert held.question == "What do you want to do with this pull request after reviewing it?"
        assert "usual merge route" in held_card.options[0].description
        assert "separate approval message" in post_card.options[0].description
        assert "not included" in post_card.checked[1]

    def test_answer_without_verified_post_approval_leaves_a_new_wait(self) -> None:
        first = ask_owner_to_approve_post(PR_URL)
        assert first is not None
        DeferredQuestion.consume(first.pk, answer="Approve separately")

        second = ask_owner_to_approve_post(PR_URL)

        assert second is not None
        assert second.pk != first.pk
        assert list(DeferredQuestion.owner_pending().values_list("pk", flat=True)) == [second.pk]

    def test_full_and_short_review_refs_share_one_post_wait(self) -> None:
        first = ask_owner_to_approve_post(PR_URL)
        second = ask_owner_to_approve_post("example/repo!12")

        assert first is not None
        assert second is not None
        assert second.pk == first.pk
        settle_live_post_wait("example/repo!12")
        first.refresh_from_db()
        assert first.status == DeferredQuestion.STATUS_DISMISSED

    def test_held_card_shows_the_review_link_to_the_owner(self) -> None:
        row = ask_owner_to_read(PR_URL, "A reviewed change", "a" * 40)
        assert row is not None
        card = QuestionCard.from_row(row.evidence, row.options_json)
        assert card is not None

        assert PR_URL in plain_text(card.view(row.question))
