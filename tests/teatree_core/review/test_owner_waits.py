"""Owner cards for waits that arise outside a parked agent task."""

from django.test import TestCase

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import QuestionCard
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.review.owner_waits import ask_owner_to_approve_post, settle_live_post_wait

PR_URL = "https://github.com/example/repo/pull/12"


class TestOwnerWaitCards(TestCase):
    def test_refused_live_post_has_one_owner_card_for_the_review(self) -> None:
        first = ask_owner_to_approve_post(PR_URL)

        assert first is not None
        assert first.evidence["decision"] == OwnerDecision.PUBLIC_POST
        assert first.evidence["subject"] == PR_URL
        assert ask_owner_to_approve_post(PR_URL).pk == first.pk
        assert DeferredQuestion.owner_pending().count() == 1

    def test_post_card_names_the_separate_approval(self) -> None:
        post = ask_owner_to_approve_post("https://github.com/example/repo/pull/13")

        assert post is not None
        post_card = QuestionCard.from_row(post.evidence, post.options_json)
        assert post_card is not None
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
