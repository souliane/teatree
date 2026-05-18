"""The recorded-approval on-behalf gate orchestration (#960/#961).

``require_on_behalf_approval`` is the single chokepoint helper every
on-behalf publish path calls. It exposes the gate's three outcomes:

* gate OFF → proceed (no approval needed);
* gate ON + recorded approval present → proceed, approval consumed + audited;
* gate ON + no recorded approval → raise :class:`OnBehalfPostBlockedError` so the
    caller surfaces the blocked post to the user (never silently drop, never
    post unattended). Default ON, fail-closed.
"""

from pathlib import Path

import pytest

from teatree.core.models.on_behalf_approval import OnBehalfApproval, OnBehalfAudit
from teatree.core.on_behalf_gate_recorded import OnBehalfPostBlockedError, require_on_behalf_approval

pytestmark = pytest.mark.django_db


def _write_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(body, encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)


class TestRecordedOnBehalfGate:
    def test_gate_off_proceeds_without_approval(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, "[teatree]\nask_before_post_on_behalf = false\n")
        require_on_behalf_approval(target="org/repo#42", action="post_comment")
        assert OnBehalfAudit.objects.count() == 0

    def test_gate_on_no_approval_blocks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, "[teatree]\nask_before_post_on_behalf = true\n")
        with pytest.raises(OnBehalfPostBlockedError) as exc:
            require_on_behalf_approval(target="org/repo#42", action="post_comment")
        # The message must tell the user exactly how to satisfy the gate (no TTY).
        assert "approve-on-behalf" in str(exc.value)
        assert "org/repo#42" in str(exc.value)

    def test_gate_on_with_recorded_approval_proceeds_and_audits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_cfg(tmp_path, monkeypatch, "[teatree]\nask_before_post_on_behalf = true\n")
        OnBehalfApproval.record(target="org/repo#42", action="post_comment", approver_id="souliane")
        require_on_behalf_approval(target="org/repo#42", action="post_comment")
        assert OnBehalfAudit.objects.filter(target="org/repo#42", action="post_comment").count() == 1
        # Single-use: a second post on the same target+action is blocked again.
        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(target="org/repo#42", action="post_comment")

    def test_gate_on_default_is_fail_closed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, "[teatree]\n")  # setting unset → default True
        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(target="t#1", action="post_comment")

    def test_recorded_approval_scope_is_exact(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, "[teatree]\nask_before_post_on_behalf = true\n")
        OnBehalfApproval.record(target="org/repo#1", action="post_comment", approver_id="souliane")
        # Wrong action — still blocked, the recorded approval does not match.
        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(target="org/repo#1", action="resolve_discussion")
