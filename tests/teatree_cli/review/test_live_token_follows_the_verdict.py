"""Under a PROCEED verdict the ``--live`` publish needs no ``LivePostApproval``.

``resolve_live_authorization`` documents a permitting posture as "no token at all is required" and
returns proceed for it, and then ``publish_live_post`` demanded the token anyway: the
path declared the post authorized and refused it one call later. Two gates disagreeing
about the same post is the defect — so one resolution now answers both questions, and
the token requirement follows the verdict instead of being asserted independently.
"""

import pytest
from django.test import TestCase

from teatree.cli.review import default_draft
from teatree.cli.review.authorize import resolve_live_authorization
from teatree.cli.review.default_draft import publish_live_post
from teatree.cli.review.service import ReviewService
from teatree.core.models import ConfigSetting, LivePostApproval, OnBehalfApproval
from tests.teatree_cli.review.conftest import OutboundHttpBan
from tests.teatree_core._on_behalf_gate_helpers import seed_forbidding_posture, seed_permitting_posture

_APPROVER = "human-operator"
_REPO = "org/repo"
_MR = 7


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in ("T3_OVERLAY_NAME", "T3_ON_BEHALF_AUTO_ACTIONS"):
        monkeypatch.delenv(env, raising=False)


class TestTheAuthorizationCarriesTheTokenRequirement(TestCase):
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)

    def test_a_permitting_posture_proceeds_and_needs_no_token(self) -> None:
        seed_permitting_posture()

        decision = resolve_live_authorization(scope=f"{_REPO}!{_MR}", action="post_comment")

        assert decision.refusal == ""
        assert decision.token_required is False

    def test_a_recorded_approval_proceeds_but_still_needs_the_token(self) -> None:
        seed_forbidding_posture()
        OnBehalfApproval.record(f"{_REPO}!{_MR}", "post_comment", _APPROVER)

        decision = resolve_live_authorization(scope=f"{_REPO}!{_MR}", action="post_comment")

        assert decision.refusal == ""
        assert decision.token_required is True

    def test_no_authorization_at_all_still_refuses(self) -> None:
        seed_forbidding_posture()

        decision = resolve_live_authorization(scope=f"{_REPO}!{_MR}", action="post_comment")

        assert "not authorized" in decision.refusal
        assert decision.token_required is True


class TestTheWaiverBelongsToThePostureNotTheVerdict(TestCase):
    """``token_required=False`` is the permitting posture's waiver, and the posture's alone.

    A PROCEED verdict is reached from several places, and only the posture is the owner's
    global "post autonomously" statement that the #1207 seal defers to. Reading the
    waiver off the verdict instead hands it to ``on_behalf_auto_actions`` too — a list
    documented for the owner's own self-documentation on their own ticket, whose
    shipped default is ``["post_e2e_evidence"]``. Naming ``post_comment`` in it would
    then publish live, colleague-visible comments under a forbidding posture, with no
    ``LivePostApproval`` and no ``OnBehalfApproval`` — a
    live-post gate retired by a setting that never mentions it.
    """

    @pytest.fixture(autouse=True)
    def _env(
        self, monkeypatch: pytest.MonkeyPatch, no_outbound_http: OutboundHttpBan, forge_reads_stubbed: None
    ) -> None:
        del forge_reads_stubbed
        _clear_env(monkeypatch)
        self.outbound = no_outbound_http

    def test_an_auto_action_proceeds_but_still_burns_the_token(self) -> None:
        seed_forbidding_posture()
        ConfigSetting.objects.set_value("on_behalf_auto_actions", ["post_e2e_evidence", "post_comment"])

        decision = resolve_live_authorization(scope=f"{_REPO}!{_MR}", action="post_comment")

        assert decision.refusal == ""
        assert decision.token_required is True

    def test_that_configuration_cannot_publish_live_without_the_token(self) -> None:
        seed_forbidding_posture()
        ConfigSetting.objects.set_value("on_behalf_auto_actions", ["post_e2e_evidence", "post_comment"])

        message, code = ReviewService(token="t").post_comment(
            _REPO, _MR, "a real finding here", live=True, file="", line=0
        )

        assert code == 1
        assert "live post blocked" in message
        assert self.outbound.attempts == 0

    def test_the_e2e_evidence_default_is_untouched_by_the_narrowing(self) -> None:
        seed_forbidding_posture()

        decision = resolve_live_authorization(scope=f"{_REPO}!{_MR}", action="post_e2e_evidence")

        assert decision.refusal == ""
        assert decision.token_required is True


class TestPublishLivePostHonoursTheRequirement(TestCase):
    def test_no_token_needed_publishes_and_consumes_nothing(self) -> None:
        result = publish_live_post(repo=_REPO, mr=_MR, publish=lambda: ("OK note_id=1", 0), token_required=False)

        assert result == ("OK note_id=1", 0)
        assert LivePostApproval.objects.count() == 0

    def test_no_token_needed_leaves_a_recorded_token_unconsumed(self) -> None:
        LivePostApproval.record(mr_url=f"{_REPO}!{_MR}", slack_ts="1700000000.0001", slack_user_id="U-OPERATOR")

        result = publish_live_post(repo=_REPO, mr=_MR, publish=lambda: ("OK note_id=1", 0), token_required=False)

        assert result == ("OK note_id=1", 0)
        assert LivePostApproval.objects.filter(consumed_at__isnull=True).count() == 1

    def test_the_requirement_defaults_on_so_a_missing_token_still_refuses(self) -> None:
        ran: list[str] = []

        message, code = publish_live_post(repo=_REPO, mr=_MR, publish=lambda: (ran.append("x"), ("OK", 0))[1])

        assert code == 1
        assert "live post blocked" in message
        assert ran == []


class TestTheServiceThreadsTheRequirementThrough(TestCase):
    @pytest.fixture(autouse=True)
    def _env(
        self, monkeypatch: pytest.MonkeyPatch, no_outbound_http: OutboundHttpBan, forge_reads_stubbed: None
    ) -> None:
        del forge_reads_stubbed
        _clear_env(monkeypatch)
        self.outbound = no_outbound_http
        self.seen: list[bool] = []

        def spy(*, repo: str, mr: int, publish: object, token_required: bool = True) -> tuple[str, int]:
            del repo, mr, publish
            self.seen.append(token_required)
            return "OK note_id=1", 0

        monkeypatch.setattr(default_draft, "publish_live_post", spy)

    def test_a_permitting_posture_publishes_with_the_token_requirement_off(self) -> None:
        seed_permitting_posture()

        _msg, code = ReviewService(token="t").post_comment(_REPO, _MR, "a real finding here", live=True)

        assert code == 0
        assert self.seen == [False]
        assert self.outbound.attempts == 0

    def test_a_forbidding_posture_keeps_the_token_requirement_on(self) -> None:
        seed_forbidding_posture()
        OnBehalfApproval.record(f"{_REPO}!{_MR}", "post_comment", _APPROVER)

        _msg, code = ReviewService(token="t").post_comment(_REPO, _MR, "a real finding here", live=True)

        assert code == 0
        assert self.seen == [True]
        assert self.outbound.attempts == 0
