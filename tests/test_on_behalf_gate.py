"""The on-behalf pre-gate: action carve-outs, and the posture that decides the rest.

``Mode.egress`` is the only control over colleague-visible output, so the verdict for an
action the carve-outs do not answer is the POSTURE's call — passed in as
*egress_forbidden* because this module is the config-only leaf every layer may import.
The ORM-side resolution (and its fail-closed read of an unreadable posture) is owned by
``tests/teatree_core/test_egress_is_the_only_colleague_control.py``.

Every case here therefore asserts BOTH polarities. The forbidding half carries the
invariant; the permitting half is what makes it falsifiable — under the retired
per-post dial a colleague-visible post BLOCKed no matter what the posture said, so a
suite that only ever asserted BLOCK could not tell the two models apart.

``get_effective_settings`` is exercised end-to-end with no mocks; the Django test DB is
the sole config tier, so the real host config never leaks in.
"""

import pytest
from django.test import TestCase

from teatree.config import Autonomy, get_effective_settings
from teatree.core.models import ConfigSetting
from teatree.on_behalf_gate import (
    OnBehalfContext,
    OnBehalfVerdict,
    on_behalf_post_will_block,
    resolve_on_behalf_verdict,
)


def _review_request_verdict(*, egress_forbidden: bool) -> OnBehalfVerdict:
    """With proved owner authorship, so the case reaches the flag and the posture."""
    return resolve_on_behalf_verdict(
        "review_request_post", OnBehalfContext(own_mr=True), egress_forbidden=egress_forbidden
    )


class _OnBehalfDbBase(TestCase):
    """Isolate the on-behalf env so the DB store is the sole config tier."""

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_ON_BEHALF_AUTO_ACTIONS"):
            monkeypatch.delenv(env, raising=False)
        self.monkeypatch = monkeypatch


class TestThePostureDecidesAColleagueVisiblePost(_OnBehalfDbBase):
    def test_a_forbidding_posture_refuses_a_colleague_visible_post(self) -> None:
        assert resolve_on_behalf_verdict("post_comment", egress_forbidden=True) is OnBehalfVerdict.BLOCK

    def test_a_permitting_posture_lets_it_through(self) -> None:
        assert resolve_on_behalf_verdict("post_comment", egress_forbidden=False) is OnBehalfVerdict.PROCEED

    def test_a_draft_is_exempt_from_both_postures(self) -> None:
        """Colleague-invisible, so the AFK line has nothing to withhold — it still DMs."""
        assert resolve_on_behalf_verdict("post_draft_note", egress_forbidden=True) is OnBehalfVerdict.AUTO_DRAFT
        assert resolve_on_behalf_verdict("post_draft_note", egress_forbidden=False) is OnBehalfVerdict.AUTO_DRAFT

    def test_review_request_actions_require_proved_owner_authorship(self) -> None:
        for action in ("review_nag_post", "review_request_resume_post", "review_request_post"):
            for own_mr in (False, None):
                with self.subTest(action=action, own_mr=own_mr):
                    ConfigSetting.objects.set_value("on_behalf_auto_actions", [action])
                    context = OnBehalfContext(own_mr=own_mr)
                    assert resolve_on_behalf_verdict(action, context, egress_forbidden=False) is OnBehalfVerdict.BLOCK

    def test_review_request_actions_proceed_with_proved_owner_authorship(self) -> None:
        for action in ("review_nag_post", "review_request_resume_post", "review_request_post"):
            with self.subTest(action=action):
                verdict = resolve_on_behalf_verdict(action, OnBehalfContext(own_mr=True), egress_forbidden=False)
                assert verdict is OnBehalfVerdict.PROCEED


class TestProactivePreCheck(_OnBehalfDbBase):
    """``on_behalf_post_will_block`` is the forward-looking BLOCK predicate.

    A caller runs it BEFORE attempting a colleague-visible post so it can offer the owner
    the switch-posture / approve-once choice proactively, instead of hitting the gate and
    only then reacting.
    """

    def test_visible_post_will_block_under_a_forbidding_posture(self) -> None:
        assert on_behalf_post_will_block("post_comment", egress_forbidden=True) is True

    def test_visible_post_will_not_block_under_a_permitting_posture(self) -> None:
        assert on_behalf_post_will_block("post_comment", egress_forbidden=False) is False

    def test_draft_form_action_will_not_block_under_either_posture(self) -> None:
        assert on_behalf_post_will_block("post_draft_note", egress_forbidden=True) is False
        assert on_behalf_post_will_block("post_draft_note", egress_forbidden=False) is False


class TestThePostureIsAlwaysStatedExplicitly(_OnBehalfDbBase):
    """Neither entry point carries a posture DEFAULT — a caller states it or gets a TypeError.

    A default is a hidden second polarity, the very thing the posture became the only
    control to remove. It would also PERMIT: an omitted argument answers "the posture
    allows this" for a caller that never asked the posture at all.
    """

    def test_the_leaf_requires_an_explicit_posture(self) -> None:
        with pytest.raises(TypeError):
            resolve_on_behalf_verdict("post_comment")  # ty: ignore[missing-argument]

    def test_the_precheck_requires_an_explicit_posture(self) -> None:
        with pytest.raises(TypeError):
            on_behalf_post_will_block("post_comment")  # ty: ignore[missing-argument]


class TestPerOverlayAutoActions(_OnBehalfDbBase):
    """The per-overlay tier still decides WHICH ACTIONS are carved out.

    What it no longer scopes is the verdict for everything else: the posture is box-global,
    so an overlay cannot open colleague egress for itself. The allowlist is the surviving
    per-overlay lever, and it is read ahead of the posture.
    """

    def test_an_overlay_scoped_allowlist_carves_its_action_out_under_either_posture(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_auto_actions", ["post_e2e_evidence"], scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")

        assert resolve_on_behalf_verdict("post_e2e_evidence", egress_forbidden=True) is OnBehalfVerdict.PROCEED

    def test_an_action_outside_the_allowlist_still_answers_to_the_posture(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_auto_actions", ["post_e2e_evidence"], scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")

        assert resolve_on_behalf_verdict("post_comment", egress_forbidden=True) is OnBehalfVerdict.BLOCK
        assert resolve_on_behalf_verdict("post_comment", egress_forbidden=False) is OnBehalfVerdict.PROCEED


class TestAutonomyTierDoesNotDecideColleagueEgress(_OnBehalfDbBase):
    """The autonomy tier is decoupled from colleague egress (#3895).

    Shipping ``autonomy = full`` says "carry the work end to end"; it does not say "speak
    as the owner to a colleague". Those are different decisions, so no tier can make a
    forbidding posture speak — and none is needed to make a permitting one speak either.
    """

    def test_shipped_full_autonomy_cannot_override_a_forbidding_posture(self) -> None:
        assert get_effective_settings().autonomy is Autonomy.FULL
        assert resolve_on_behalf_verdict("post_comment", egress_forbidden=True) is OnBehalfVerdict.BLOCK

    def test_shipped_full_autonomy_needs_no_extra_opt_in_under_a_permitting_posture(self) -> None:
        assert get_effective_settings().autonomy is Autonomy.FULL
        assert resolve_on_behalf_verdict("post_comment", egress_forbidden=False) is OnBehalfVerdict.PROCEED

    def test_an_explicit_full_overlay_cannot_override_a_forbidding_posture(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")

        assert resolve_on_behalf_verdict("post_comment", egress_forbidden=True) is OnBehalfVerdict.BLOCK
        assert resolve_on_behalf_verdict("post_comment", egress_forbidden=False) is OnBehalfVerdict.PROCEED

    def test_an_explicit_notify_overlay_cannot_override_a_forbidding_posture(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "notify", scope="client")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "client")

        assert resolve_on_behalf_verdict("post_comment", egress_forbidden=True) is OnBehalfVerdict.BLOCK
        assert resolve_on_behalf_verdict("post_comment", egress_forbidden=False) is OnBehalfVerdict.PROCEED


class TestReviewRequestPostAnswersToItsOwnFlag(_OnBehalfDbBase):
    """The one action decided by a setting rather than the posture — it is action-shaped.

    ``review_request_post_disabled`` says "this ACTION is off", so it BLOCKs even where the
    posture would happily speak. Without the second assertion the first could not tell the
    flag from the posture.
    """

    def test_the_flag_blocks_even_under_a_permitting_posture(self) -> None:
        ConfigSetting.objects.set_value("review_request_post_disabled", value=True)

        assert _review_request_verdict(egress_forbidden=False) is OnBehalfVerdict.BLOCK

    def test_without_the_flag_the_posture_decides_it_like_any_other_action(self) -> None:
        assert _review_request_verdict(egress_forbidden=False) is OnBehalfVerdict.PROCEED
        assert _review_request_verdict(egress_forbidden=True) is OnBehalfVerdict.BLOCK
