"""Inline post downgraded to a position-less note must hard-fail (#1161).

When GitLab silently downgrades an inline ``discussions`` POST to an
MR-LEVEL note (the response's ``notes[0].position`` is null / absent /
lacks ``new_path``), the comment is live but NOT anchored on the diff.
The earlier M3 fix returned ``rc=0`` here to avoid a double-posting retry;
the EXTREMELY RED CARD (#1161) supersedes that: a degraded post must
REFUSE the success (``rc=1``, clear error) so a sub-agent cannot report
"inline POSTs succeeded" while every post was MR-level.

The double-post concern M3 raised is preserved a different way: the claim
is still recorded on the degraded path, so a retry is a no-op against the
ledger rather than a duplicate. ``rc=1`` stays reserved for "did not
anchor inline" AND for genuine post FAILURES (``post_json`` falsy).

These tests call ``post_comment_impl`` directly to avoid the live-post
authorization chain which is irrelevant to this code path.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from teatree.cli.review.post_impl import post_comment_impl
from teatree.core.models import OutboundClaim

pytestmark = pytest.mark.django_db


def _make_service(*, resolve_base_url: str = "https://gitlab.example/api/v4") -> MagicMock:
    """Minimal ReviewService stand-in for post_comment_impl."""
    service = MagicMock()
    service._resolve_base_url = lambda: resolve_base_url
    return service


def _make_api(*, note_type: str) -> MagicMock:
    """GitLabAPI stub whose inline post returns a position-less (downgraded) note."""
    api = MagicMock()
    api.post_json.return_value = {
        "id": "disc-99",
        "notes": [
            {
                "type": note_type,
                "id": 77,
                "web_url": "https://gitlab.example/org/repo/-/mr/7#note_77",
            },
        ],
    }
    return api


def _patch_position(*, file: str = "src/foo.py", line: int = 42):
    """Patch resolve_inline_position to return a valid position without network."""
    return patch(
        "teatree.cli.review.post_impl.resolve_inline_position",
        return_value=({"base_sha": "abc", "head_sha": "def", "start_sha": "ghi"}, None),
    )


class TestPostCommentImplNonDiffNote:
    """Inline post downgraded to a position-less note must hard-fail but still record the claim (#1161)."""

    def test_non_diffnote_post_records_claim(self, tmp_path: Path) -> None:
        service = _make_service()
        api = _make_api(note_type="TextDiffNote")
        service._get_api.return_value = api

        with _patch_position():
            msg, code = post_comment_impl(service, "org/repo", 7, "nit", file="src/foo.py", line=42)

        assert code == 1, f"position-less downgrade must refuse success (#1161), got rc={code!r}, msg={msg!r}"
        assert OutboundClaim.objects.filter(
            idempotency_key__startswith="gitlab_note:org/repo!7:",
        ).exists(), "record_note_claim must still run on the degraded path so a retry is idempotent"

    def test_non_diffnote_post_message_signals_degradation(self) -> None:
        service = _make_service()
        api = _make_api(note_type="Note")
        service._get_api.return_value = api

        with _patch_position():
            msg, code = post_comment_impl(service, "org/repo", 7, "nit", file="src/foo.py", line=42)

        assert code == 1
        # The message must name the MR-level downgrade so the caller knows it wasn't anchored inline.
        assert "MR-level" in msg or "not anchored" in msg.lower(), (
            f"message should signal the MR-level downgrade, got: {msg!r}"
        )

    def test_genuine_failure_still_returns_rc1(self) -> None:
        """``post_json`` returning None is a real failure — rc must stay 1 and record no claim."""
        service = _make_service()
        api = MagicMock()
        api.post_json.return_value = None
        service._get_api.return_value = api

        with _patch_position():
            _msg, code = post_comment_impl(service, "org/repo", 7, "nit", file="src/foo.py", line=42)

        assert code == 1
        assert not OutboundClaim.objects.filter(
            idempotency_key__startswith="gitlab_note:org/repo!7:",
        ).exists()
