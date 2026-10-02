"""``Mode.egress`` alone decides whether the owner's voice leaves the box (B6).

The preset is the single surface for factory behaviour, so a posture's egress opinion is
the WHOLE answer for a colleague-visible on-behalf action. A second dial orthogonal to the
posture could disagree with it, and the pair had no defined resolution — ``present`` meant
"do everything" while the per-post dial still refused to speak.

Nothing here names a per-post setting: these assertions must hold from the posture alone.
"""

from unittest.mock import patch

import django.test
import pytest

from teatree.core.mode_resolution import ResolvedMode, egress_forbidden, owner_voice_forbidden
from teatree.core.models import Mode, ModeOverride
from teatree.core.on_behalf_gate_recorded import (
    OnBehalfPostBlockedError,
    on_behalf_block_message,
    require_on_behalf_approval,
    resolve_posture_verdict,
)
from teatree.on_behalf_gate import OnBehalfVerdict, resolve_on_behalf_verdict

#: Colleague-visible and outside every carve-out, so the posture is what decides it.
_COLLEAGUE_ACTION = "approve"


def _pin_posture(name: str, egress: str) -> None:
    Mode.objects.update_or_create(name=name, defaults={"entries": {}, "egress": egress})
    ModeOverride.objects.set_override(name, reason=f"pinning {name!r} for this test")


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestEgressAloneDecidesColleagueEgress(django.test.TestCase):
    def setUp(self) -> None:
        ModeOverride.objects.all().delete()

    def test_a_colleague_action_proceeds_under_a_permitting_posture(self) -> None:
        _pin_posture("present", "allow")

        assert resolve_posture_verdict(_COLLEAGUE_ACTION) is OnBehalfVerdict.PROCEED

    def test_a_colleague_action_is_refused_under_a_forbidding_posture(self) -> None:
        _pin_posture("afk", "forbid")

        assert resolve_posture_verdict(_COLLEAGUE_ACTION) is OnBehalfVerdict.BLOCK

    def test_an_ordinary_posture_refusal_logs_no_error(self) -> None:
        """Every AFK refusal is the designed per-action line, not a bug report about a loop."""
        _pin_posture("afk", "forbid")

        with self.assertNoLogs("teatree.core.on_behalf_gate_recorded", level="ERROR"):
            assert resolve_posture_verdict(_COLLEAGUE_ACTION) is OnBehalfVerdict.BLOCK

    def test_a_final_posture_refusal_logs_no_error(self) -> None:
        _pin_posture("afk", "forbid")

        with self.assertNoLogs("teatree.core.on_behalf_gate_recorded", level="ERROR"):
            assert on_behalf_block_message("acme/alpha!7", _COLLEAGUE_ACTION) != ""
            with pytest.raises(OnBehalfPostBlockedError):
                require_on_behalf_approval(target="acme/alpha!7", action=_COLLEAGUE_ACTION, publish=lambda: "posted")


class TestAnUnreadablePostureIsNotPermission(django.test.TestCase):
    def test_a_posture_that_cannot_be_resolved_refuses_the_owners_voice(self) -> None:
        """Unknown is not permission to speak as someone else."""
        unreadable = ResolvedMode(
            mode=Mode(name="present", entries={}), source="default", until=None, reason="unreadable", fail_open=True
        )
        with patch("teatree.core.mode_resolution.resolve_active_mode", return_value=unreadable):
            assert owner_voice_forbidden() is True

    def test_an_unresolvable_posture_still_lets_the_factory_build_its_own_work(self) -> None:
        """The opposite polarity, on the same unreadable posture (SELECTION, not speech).

        Failing this one closed stops the review loop WIRING its colleague scanners, which
        mutes the factory's work rather than its voice — caught by the loop-classification
        conformance lane, not by any assertion here before it existed.
        """
        unreadable = ResolvedMode(
            mode=Mode(name="present", entries={}), source="default", until=None, reason="unreadable", fail_open=True
        )
        with patch("teatree.core.mode_resolution.resolve_active_mode", return_value=unreadable):
            assert egress_forbidden() is False


class TestADraftStillEarnsItsReceipt(django.test.TestCase):
    """A draft is colleague-invisible, so no posture refuses it — but it is not silent.

    ``AUTO_DRAFT`` is what drives the DM naming the publish/delete commands
    (``on_behalf_gate_recorded`` acts on that verdict alone). Collapsing drafts to
    ``PROCEED`` publishes the draft and tells the owner nothing, which is how the draft-first
    receipt would be lost while every gate test still passed.
    """

    def test_a_draft_form_action_auto_drafts_rather_than_proceeding_silently(self) -> None:
        assert resolve_on_behalf_verdict("post_draft_note", egress_forbidden=False) is OnBehalfVerdict.AUTO_DRAFT

    def test_a_forbidding_posture_still_does_not_refuse_a_draft(self) -> None:
        verdict = resolve_on_behalf_verdict("post_draft_note", egress_forbidden=True)

        assert verdict is OnBehalfVerdict.AUTO_DRAFT
