"""#119: the on-behalf gate consults the approval dial for the ``on_behalf_post`` class.

Under a blocking mode with no recorded human approval, a graduated ``on_behalf_post``
owner-taint post proceeds by policy — recording a single-use ``policy`` approval and its
audit, exactly as a human approval would. An untrusted taint is floored to BLOCK; an
ungraduated class BLOCKs unchanged (inert at ship).
"""

from pathlib import Path

import pytest

from teatree.core.models import ConfigSetting
from teatree.core.models.approval_dial import DIAL_CONFIG_KEY
from teatree.core.models.on_behalf_approval import OnBehalfApproval, OnBehalfAudit
from teatree.core.models.provenance import Provenance
from teatree.core.on_behalf_gate_recorded import (
    OnBehalfPartialPublishError,
    OnBehalfPostBlockedError,
    on_behalf_block_message,
    require_on_behalf_approval,
)
from teatree.on_behalf_gate import OnBehalfContext
from tests.teatree_core._on_behalf_gate_helpers import seed_forbidding_posture

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _forbidding_posture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
    seed_forbidding_posture()


def _graduate_on_behalf() -> None:
    ConfigSetting.objects.set_value(DIAL_CONFIG_KEY, {"on_behalf_post": "auto"}, scope="")


def _publish() -> str:
    return "posted"


class TestOnBehalfDialGraduation:
    def test_ungraduated_still_blocks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _forbidding_posture(tmp_path, monkeypatch)
        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(target="org/repo#1", action="post_comment", publish=_publish)
        assert OnBehalfAudit.objects.count() == 0

    def test_graduated_owner_taint_proceeds_and_leaves_a_policy_audit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _forbidding_posture(tmp_path, monkeypatch)
        _graduate_on_behalf()
        result = require_on_behalf_approval(target="org/repo#1", action="post_comment", publish=_publish)
        assert result == "posted"
        audit = OnBehalfAudit.objects.get()
        assert audit.approver_id == "policy"
        assert audit.action == "post_comment"
        # The policy approval is single-use — a second post is not free.
        assert OnBehalfApproval.objects.filter(consumed_at__isnull=True).count() == 0

    def test_graduated_but_untrusted_taint_is_floored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _forbidding_posture(tmp_path, monkeypatch)
        _graduate_on_behalf()
        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(
                target="org/repo#1", action="post_comment", publish=_publish, taint=Provenance.PUBLIC.value
            )

    def test_peek_reports_may_proceed_when_graduated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _forbidding_posture(tmp_path, monkeypatch)
        _graduate_on_behalf()
        assert on_behalf_block_message("org/repo#1", "post_comment") == ""

    def test_peek_still_blocks_untrusted_taint(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _forbidding_posture(tmp_path, monkeypatch)
        _graduate_on_behalf()
        assert on_behalf_block_message("org/repo#1", "post_comment", taint=Provenance.PUBLIC.value) != ""

    @pytest.mark.parametrize("action", ["review_nag_post", "review_request_resume_post", "review_request_post"])
    def test_authorship_refusal_cannot_be_approved_or_graduated(
        self, action: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _forbidding_posture(tmp_path, monkeypatch)
        _graduate_on_behalf()
        approval = OnBehalfApproval.record("org/repo#1", action, "owner")
        context = OnBehalfContext(own_mr=False, target="org/repo#1")
        published: list[str] = []

        message = on_behalf_block_message("org/repo#1", action, context=context)
        assert "authorship" in message
        assert "approve-on-behalf" not in message
        with pytest.raises(OnBehalfPostBlockedError, match="authorship"):
            require_on_behalf_approval(
                target="org/repo#1",
                action=action,
                context=context,
                publish=lambda: published.append("posted"),
            )

        approval.refresh_from_db()
        assert approval.consumed_at is None
        assert published == []


class TestAPartialPublishUnderPolicyGraduation:
    """A graduated post that lands some of its artifacts and then fails.

    The rollback takes the policy approval with it — that row was RECORDED inside the
    same block — so the audit for what landed has no subject left to point at unless it
    is re-recorded. Without that, this is the one path where keeping the audit raises
    an integrity error instead of writing one.
    """

    def test_the_landed_posts_are_audited_though_the_grant_rolled_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _forbidding_posture(tmp_path, monkeypatch)
        _graduate_on_behalf()

        def partial() -> str:
            raise OnBehalfPartialPublishError(("Posted 1 of 2; Failed to post comment", 1))

        with pytest.raises(OnBehalfPartialPublishError):
            require_on_behalf_approval(
                target="org/repo#1", action="post_comment", publish=partial, taint=Provenance.OWNER.value
            )

        assert OnBehalfAudit.objects.count() == 1
        assert OnBehalfAudit.objects.get().approver_id == "policy"
