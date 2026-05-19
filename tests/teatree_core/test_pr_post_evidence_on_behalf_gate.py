"""``t3 pr post-evidence`` is on-behalf-gated under the tri-state mode (#960).

``post-evidence`` publishes a comment under the user's identity on a
colleague-visible PR/MR — it is a third on-behalf chokepoint alongside
``_BaseReplier`` (reply transport) and ``ReviewService`` (review CLI),
so it routes through the same satisfiable ``on_behalf_post_mode`` gate:

*   ``IMMEDIATE`` → publish (no approval needed);
*   ``ASK`` / ``DRAFT_OR_ASK`` + no approval → no host call (the upload +
    comment paths MUST NOT fire); the command returns an actionable
    ``approve-on-behalf`` error message;
*   ``ASK`` / ``DRAFT_OR_ASK`` + a recorded :class:`OnBehalfApproval`
    scoped to ``(<repo>!<mr>, "post_evidence")`` → consume the row, write
    an audit, proceed to upload + post.

The action is NOT a draft-form action: it BLOCKs identically under ASK
and DRAFT_OR_ASK.

The gate is inlined at the ``post_evidence`` command — not at the
``code_host`` layer — so PR *creation* (which is not an on-behalf
colleague-facing post) remains ungated.
"""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings

import teatree.core.management.commands.pr as pr_mod
from teatree.config import OnBehalfPostMode
from teatree.core.models import OnBehalfApproval, OnBehalfAudit
from tests.teatree_core.management_commands._overlays import FULL_OVERLAY, SETTINGS, _patch_overlays

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


def _gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mode: OnBehalfPostMode) -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        f'[teatree]\non_behalf_post_mode = "{mode.value}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)


_BLOCKING_MODES = [OnBehalfPostMode.ASK, OnBehalfPostMode.DRAFT_OR_ASK]


class TestPostEvidenceOnBehalfGate(TestCase):
    """A blocking mode without an approval refuses the post at the command layer."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_post_evidence_blocked_under_ask_no_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.ASK)
        mock_host = MagicMock()

        with patch.object(pr_mod, "code_host_from_overlay", return_value=mock_host):
            result = cast(
                "dict[str, object]",
                call_command(
                    "pr",
                    "post-evidence",
                    "100",
                    repo="my/repo",
                    body="proof",
                ),
            )

        assert "error" in result
        assert "approve-on-behalf" in str(result["error"])
        # Load-bearing: the host stub MUST NOT have been called at all —
        # no upload, no list, no post, no update. Routing through the gate
        # short-circuits BEFORE any colleague-visible side effect.
        assert mock_host.method_calls == [], (
            f"host was called despite the gate blocking the post: {mock_host.method_calls!r}"
        )

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_post_evidence_blocked_under_draft_or_ask_no_approval(self) -> None:
        """``post_evidence`` is NOT a draft-form action — DRAFT_OR_ASK still BLOCKs."""
        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.DRAFT_OR_ASK)
        mock_host = MagicMock()

        with patch.object(pr_mod, "code_host_from_overlay", return_value=mock_host):
            result = cast(
                "dict[str, object]",
                call_command(
                    "pr",
                    "post-evidence",
                    "100",
                    repo="my/repo",
                    body="proof",
                ),
            )

        assert "error" in result
        assert "approve-on-behalf" in str(result["error"])
        assert mock_host.method_calls == []

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_post_evidence_passes_under_immediate(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.IMMEDIATE)
        mock_host = MagicMock()
        mock_host.post_pr_comment.return_value = {"id": 7}
        mock_host.list_pr_comments.return_value = []

        with patch.object(pr_mod, "code_host_from_overlay", return_value=mock_host):
            result = cast(
                "dict[str, object]",
                call_command(
                    "pr",
                    "post-evidence",
                    "100",
                    repo="my/repo",
                    body="proof",
                ),
            )

        assert result == {"id": 7}
        mock_host.post_pr_comment.assert_called_once()

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_post_evidence_passes_with_recorded_approval_under_ask(self) -> None:
        self._exercise_approval_path(OnBehalfPostMode.ASK)

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_post_evidence_passes_with_recorded_approval_under_draft_or_ask(self) -> None:
        self._exercise_approval_path(OnBehalfPostMode.DRAFT_OR_ASK)

    def _exercise_approval_path(self, mode: OnBehalfPostMode) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
        OnBehalfApproval.record(target="my/repo!100", action="post_evidence", approver_id="souliane")
        mock_host = MagicMock()
        mock_host.post_pr_comment.return_value = {"id": 8}
        mock_host.list_pr_comments.return_value = []

        with patch.object(pr_mod, "code_host_from_overlay", return_value=mock_host):
            result = cast(
                "dict[str, object]",
                call_command(
                    "pr",
                    "post-evidence",
                    "100",
                    repo="my/repo",
                    body="proof",
                ),
            )

        assert result == {"id": 8}
        # The approval was consumed and an audit row was written.
        approval = OnBehalfApproval.objects.get(target="my/repo!100", action="post_evidence")
        assert approval.consumed_at is not None
        assert OnBehalfAudit.objects.filter(approval=approval).count() == 1
        mock_host.post_pr_comment.assert_called_once()
