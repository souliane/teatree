"""Regression tests for the POSITIVE independent-checker predicate (#4241).

maker≠checker used to be enforced by a denylist of maker role words, so every
unrecognised free-text ``reviewer_identity`` classified as an independent reviewer —
including ``ac-reviewing-codebase`` / ``architectural_review``, the identities the
periodic holistic review pass carries now that it authors its own PR (#4230). The
predicate is inverted here: an identity is admitted only when it positively identifies
a reviewer role (or is a configured human), and refused otherwise.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config.reviewer_identities import effective_independent_reviewer_identities
from teatree.config.settings import UserSettings
from teatree.core.merge.authorization import assert_review_verdict_gate
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.models import MergeClear, ReviewVerdict
from teatree.core.models.merge_clear import ClearIssuanceError, ClearRequest, diff_paths_are_substrate
from teatree.core.models.review_verdict import ReviewVerdictError
from teatree.core.models.reviewer_identity import is_independent_reviewer_identity, is_non_reviewer_role

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "e" * 40
_RESOLVER = "teatree.core.models.reviewer_identity._configured_reviewer_identities"

#: The identities a review pass that implements its own findings actually carries.
REVIEW_AUTHORING_IDENTITIES = ("ac-reviewing-codebase", "architectural_review", "architectural-review")


class TestReviewAuthoringIdentitiesRefused(TestCase):
    """The #4230 review-authoring identities are makers, not independent reviewers."""

    def test_predicate_refuses_each(self) -> None:
        for identity in REVIEW_AUTHORING_IDENTITIES:
            assert is_independent_reviewer_identity(identity) is False, identity

    def test_denylist_positively_names_each(self) -> None:
        # Not merely "unrecognised": renaming one to `cold-architectural-review`
        # must not buy it a reviewer role token.
        for identity in (*REVIEW_AUTHORING_IDENTITIES, "cold-architectural-review"):
            assert is_non_reviewer_role(identity) is True, identity

    def test_verdict_record_refuses_each(self) -> None:
        for identity in REVIEW_AUTHORING_IDENTITIES:
            with pytest.raises(ReviewVerdictError, match="independent"):
                ReviewVerdict.record(
                    pr_id=4241,
                    slug="souliane/teatree",
                    reviewed_sha=_SHA,
                    verdict=ReviewVerdict.Verdict.MERGE_SAFE.value,
                    reviewer_identity=identity,
                )
        assert ReviewVerdict.objects.count() == 0

    def test_merge_gate_still_refuses_after_a_self_authored_attempt(self) -> None:
        for identity in REVIEW_AUTHORING_IDENTITIES:
            with pytest.raises(ReviewVerdictError):
                ReviewVerdict.record(
                    pr_id=4241,
                    slug="souliane/teatree",
                    reviewed_sha=_SHA,
                    verdict=ReviewVerdict.Verdict.MERGE_SAFE.value,
                    reviewer_identity=identity,
                )
        with pytest.raises(MergePreconditionError, match="no recorded merge_safe"):
            assert_review_verdict_gate(slug="souliane/teatree", pr_id=4241, head_sha=_SHA)

    def test_clear_issuance_refuses_each(self) -> None:
        for identity in REVIEW_AUTHORING_IDENTITIES:
            with pytest.raises(ClearIssuanceError, match="reviewer"):
                MergeClear.issue(
                    ClearRequest(
                        pr_id=4241,
                        slug="souliane/teatree",
                        reviewed_sha=_SHA,
                        reviewer_identity=identity,
                        executing_loop_identity="merge-loop",
                        gh_verify_result="green",
                        blast_class="logic",
                    )
                )
        assert MergeClear.objects.count() == 0


class TestUnrecognisedIdentityFailsClosed(TestCase):
    """A novel identity is REFUSED rather than admitted — the inverted default."""

    def test_novel_identity_is_refused(self) -> None:
        assert is_independent_reviewer_identity("quarterly-drift-sweeper") is False

    def test_novel_identity_is_not_on_the_denylist(self) -> None:
        # It is refused for want of a positive role, not because it was enumerated —
        # this is what makes the default fail-CLOSED rather than fail-open.
        assert is_non_reviewer_role("quarterly-drift-sweeper") is False

    def test_blank_identity_is_refused(self) -> None:
        assert is_independent_reviewer_identity("   ") is False

    def test_verdict_record_refuses_a_novel_identity(self) -> None:
        with pytest.raises(ReviewVerdictError, match="independent"):
            ReviewVerdict.record(
                pr_id=4242,
                slug="souliane/teatree",
                reviewed_sha=_SHA,
                verdict=ReviewVerdict.Verdict.MERGE_SAFE.value,
                reviewer_identity="quarterly-drift-sweeper",
            )


class TestRecognisedReviewerRolesAdmitted(TestCase):
    """The factory's own reviewer identities keep working — the gate must not stall."""

    def test_role_component_identities_are_admitted(self) -> None:
        for identity in (
            "cold-reviewer",
            "t3:reviewer",
            "headless-reviewer",
            "claude-cold-review",
            "orchestrator-cold-review",
            "cold-reviewer/pr3398-fable-20260718",
            "reviewer:claude-cold-review",
            "cr/prd",
            "an-independent-critic",
            "adjudicator-4230-verdict-contradiction",
        ):
            assert is_independent_reviewer_identity(identity) is True, identity

    def test_case_and_whitespace_variants_are_admitted(self) -> None:
        assert is_independent_reviewer_identity("  Cold-Reviewer  ") is True

    def test_verdict_record_admits_a_recognised_reviewer(self) -> None:
        recorded = ReviewVerdict.record(
            pr_id=4243,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict=ReviewVerdict.Verdict.MERGE_SAFE.value,
            reviewer_identity="cold-reviewer",
        )
        assert recorded.pk is not None
        assert_review_verdict_gate(slug="souliane/teatree", pr_id=4243, head_sha=_SHA)


class TestDenylistOverridesTheAllowlist(TestCase):
    """A maker cannot buy admission by bolting a reviewer word onto its name."""

    def test_maker_prefixed_reviewer_is_refused(self) -> None:
        for identity in ("maker-cold-reviewer", "coding-agent-cold-reviewer", "merge-loop-reviewer"):
            assert is_non_reviewer_role(identity) is True, identity
            assert is_independent_reviewer_identity(identity) is False, identity


class TestConfiguredHumanIdentitiesAdmitted(TestCase):
    """A human owner handle is the strongest independent review — and carries no role token."""

    def test_owner_alias_admitted_when_configured(self) -> None:
        with patch(_RESOLVER, return_value=frozenset({"souliane"})):
            assert is_independent_reviewer_identity("souliane") is True
            assert is_independent_reviewer_identity("Souliane") is True

    def test_unconfigured_human_handle_is_refused(self) -> None:
        with patch(_RESOLVER, return_value=frozenset()):
            assert is_independent_reviewer_identity("souliane") is False

    def test_configured_identity_cannot_override_the_denylist(self) -> None:
        with patch(_RESOLVER, return_value=frozenset({"merge-loop"})):
            assert is_independent_reviewer_identity("merge-loop") is False


class TestThePredicateModuleStaysSubstrate(TestCase):
    """Splitting the primitives out must not de-classify the trust boundary they define."""

    def test_the_reviewer_identity_module_is_a_substrate_path(self) -> None:
        assert diff_paths_are_substrate(["src/teatree/core/models/reviewer_identity.py"]) is True

    def test_a_look_alike_sibling_is_not(self) -> None:
        assert diff_paths_are_substrate(["src/teatree/core/models/reviewer_identity_helpers.py"]) is False


class TestConfigResolver(TestCase):
    """``effective_independent_reviewer_identities`` — the config tier feeding the predicate."""

    def test_unions_owner_aliases_and_the_explicit_allowlist(self) -> None:
        settings = UserSettings(
            user_identity_aliases=["Souliane", " "],
            independent_reviewer_identities=["Trusted  Human", ""],
        )
        assert effective_independent_reviewer_identities(settings) == frozenset({"souliane", "trusted human"})

    def test_unconfigured_deployment_resolves_empty(self) -> None:
        assert effective_independent_reviewer_identities(UserSettings()) == frozenset()
