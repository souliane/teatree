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
    OnBehalfPostBlockedError,
    on_behalf_block_message,
    require_on_behalf_approval,
)

pytestmark = pytest.mark.django_db


def _ask_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text("[teatree]\n", encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    monkeypatch.setenv("T3_ON_BEHALF_POST_MODE", "ask")
    monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)


def _graduate_on_behalf() -> None:
    ConfigSetting.objects.set_value(DIAL_CONFIG_KEY, {"on_behalf_post": "auto"}, scope="")


def _publish() -> str:
    return "posted"


class TestOnBehalfDialGraduation:
    def test_ungraduated_still_blocks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _ask_mode(tmp_path, monkeypatch)
        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(target="org/repo#1", action="post_comment", publish=_publish)
        assert OnBehalfAudit.objects.count() == 0

    def test_graduated_owner_taint_proceeds_and_leaves_a_policy_audit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ask_mode(tmp_path, monkeypatch)
        _graduate_on_behalf()
        result = require_on_behalf_approval(target="org/repo#1", action="post_comment", publish=_publish)
        assert result == "posted"
        audit = OnBehalfAudit.objects.get()
        assert audit.approver_id == "policy"
        assert audit.action == "post_comment"
        # The policy approval is single-use — a second post is not free.
        assert OnBehalfApproval.objects.filter(consumed_at__isnull=True).count() == 0

    def test_graduated_but_untrusted_taint_is_floored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _ask_mode(tmp_path, monkeypatch)
        _graduate_on_behalf()
        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(
                target="org/repo#1", action="post_comment", publish=_publish, taint=Provenance.PUBLIC.value
            )

    def test_peek_reports_may_proceed_when_graduated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _ask_mode(tmp_path, monkeypatch)
        _graduate_on_behalf()
        assert on_behalf_block_message("org/repo#1", "post_comment") == ""

    def test_peek_still_blocks_untrusted_taint(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _ask_mode(tmp_path, monkeypatch)
        _graduate_on_behalf()
        assert on_behalf_block_message("org/repo#1", "post_comment", taint=Provenance.PUBLIC.value) != ""
