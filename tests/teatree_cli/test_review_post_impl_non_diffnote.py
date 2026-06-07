"""M3 regression: non-DiffNote inline post must record the claim and not signal retry.

When GitLab downgrades a DiffNote to a TextDiffNote/Note (because the MR
head/base moved), the comment IS live on GitLab but the old code returned
``rc=1`` without calling ``record_note_claim`` or
``notify_review_after_receipt``.  The caller would then retry, double-posting.

Safe-direction fix: on a non-DiffNote SUCCESS, still call
``record_note_claim`` and ``notify_review_after_receipt``, return rc=0 with
a warning that it wasn't anchored inline.  rc=1 stays reserved for genuine
post FAILURES (``post_json`` returning None/falsy).

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
    """GitLabAPI stub whose inline post returns a discussion with note_type."""
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
    """Inline post downgraded to non-DiffNote must record claim and return rc=0."""

    def test_non_diffnote_post_records_claim(self, tmp_path: Path) -> None:
        service = _make_service()
        api = _make_api(note_type="TextDiffNote")
        service._get_api.return_value = api

        with _patch_position():
            msg, code = post_comment_impl(service, "org/repo", 7, "nit", file="src/foo.py", line=42)

        assert code == 0, f"expected rc=0 for non-DiffNote success (post is live), got rc={code!r}, msg={msg!r}"
        assert OutboundClaim.objects.filter(
            idempotency_key__startswith="gitlab_note:org/repo!7:",
        ).exists(), "record_note_claim must be called even when GitLab downgrades to non-DiffNote"

    def test_non_diffnote_post_message_warns_not_anchored(self) -> None:
        service = _make_service()
        api = _make_api(note_type="Note")
        service._get_api.return_value = api

        with _patch_position():
            msg, code = post_comment_impl(service, "org/repo", 7, "nit", file="src/foo.py", line=42)

        assert code == 0
        # The message must signal the downgrade so the caller knows it wasn't anchored inline.
        assert "not anchored" in msg.lower() or "type=" in msg, (
            f"message should warn about non-DiffNote downgrade, got: {msg!r}"
        )

    def test_genuine_failure_still_returns_rc1(self) -> None:
        """``post_json`` returning None is a real failure — rc must stay 1."""
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
