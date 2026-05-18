"""``t3 pr post-evidence`` is on-behalf-gated (#960).

``post-evidence`` publishes a comment under the user's identity on a
colleague-visible PR/MR — it is a third on-behalf chokepoint alongside
``_BaseReplier`` (reply transport) and ``ReviewService`` (review CLI),
so it routes through the same satisfiable recorded-approval pre-gate:

* gate ON + no approval → no host call (the upload + comment paths MUST
    NOT fire); the command returns an actionable ``approve-on-behalf``
    error message;
* gate ON + a recorded :class:`OnBehalfApproval` scoped to
    ``(<repo>!<mr>, "post_evidence")`` → consume the row, write an audit,
    proceed to upload + post;
* gate OFF → behaves exactly as before.

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
from teatree.core.models import OnBehalfApproval, OnBehalfAudit
from tests.teatree_core.management_commands._overlays import FULL_OVERLAY, SETTINGS, _patch_overlays

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


def _gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        f"[teatree]\nask_before_post_on_behalf = {'true' if on else 'false'}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)


class TestPostEvidenceOnBehalfGate(TestCase):
    """Gate ON without an approval refuses the post at the command layer."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_post_evidence_blocked_when_gate_on_no_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
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
    def test_post_evidence_passes_when_gate_off(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=False)
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
    def test_post_evidence_passes_with_recorded_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
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
