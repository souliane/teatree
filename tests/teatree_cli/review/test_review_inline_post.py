"""Inline-post wrapper must verify ``notes[0].position.new_path`` and hard-fail degradation (#1161).

The EXTREMELY RED CARD: GitLab silently accepts an inline-anchored
``discussions`` POST as an MR-LEVEL note when the requested position does
not fall on a ``+``-added or context line of the diff hunk — the response
JSON simply omits ``position`` (or returns it null / without
``new_path``). The old code returned ``rc=0`` with a soft warning, so a
sub-agent reported "inline POSTs succeeded" while every post was MR-level.

The fix: after the POST, parse ``notes[0].position``. If it is null /
absent / has no ``new_path``, REFUSE the success — return ``rc=1`` with a
clear error. The claim is still recorded (idempotency ledger) so a retry
is a no-op rather than a double-post; the non-zero exit code is what stops
the caller from claiming an inline anchor that did not happen.

These tests drive ``post_comment_impl`` directly (the live inline path)
with a mocked GitLab response so both branches — a degraded position-less
note and a genuine inline DiffNote — are covered without the network.
"""

from unittest.mock import MagicMock, patch

import pytest

from teatree.cli.review.post_impl import post_comment_impl
from teatree.core.models import OutboundClaim

pytestmark = pytest.mark.django_db


def _make_service() -> MagicMock:
    service = MagicMock()
    service._resolve_base_url = lambda: "https://gitlab.example/api/v4"
    return service


def _make_api(*, note: dict) -> MagicMock:
    """GitLabAPI stub whose inline POST returns a discussion with one *note*."""
    api = MagicMock()
    api.post_json.return_value = {"id": "disc-1", "notes": [note]}
    return api


def _patch_position():
    return patch(
        "teatree.cli.review.post_impl.resolve_inline_position",
        return_value=({"base_sha": "a", "head_sha": "b", "start_sha": "c"}, None),
    )


class TestInlinePostVerifiesNewPath:
    """An inline post that GitLab downgraded to MR-level must hard-fail (#1161)."""

    def test_position_null_refuses_success(self) -> None:
        """``notes[0].position is None`` (silent MR-level downgrade) → rc=1, clear error."""
        service = _make_service()
        service._get_api.return_value = _make_api(
            note={"type": "Note", "id": 51, "position": None, "web_url": "u"},
        )

        with _patch_position():
            msg, code = post_comment_impl(service, "org/repo", 7, "nit", file="src/foo.py", line=42)

        assert code == 1, f"position-null degradation must refuse success, got rc={code!r} msg={msg!r}"
        assert "MR-level" in msg or "not anchored" in msg.lower()
        assert "src/foo.py" in msg
        assert "42" in msg

    def test_position_without_new_path_refuses_success(self) -> None:
        """A ``position`` object lacking ``new_path`` is still a degradation → rc=1."""
        service = _make_service()
        service._get_api.return_value = _make_api(
            note={"type": "Note", "id": 52, "position": {"new_line": 42}, "web_url": "u"},
        )

        with _patch_position():
            msg, code = post_comment_impl(service, "org/repo", 7, "nit", file="src/foo.py", line=42)

        assert code == 1, f"position without new_path must refuse success, got rc={code!r} msg={msg!r}"

    def test_position_absent_refuses_success(self) -> None:
        """``position`` field entirely absent from the note → rc=1."""
        service = _make_service()
        service._get_api.return_value = _make_api(note={"type": "Note", "id": 53, "web_url": "u"})

        with _patch_position():
            _msg, code = post_comment_impl(service, "org/repo", 7, "nit", file="src/foo.py", line=42)

        assert code == 1

    def test_degraded_post_still_records_claim(self) -> None:
        """The claim is recorded even on refusal so a retry is a no-op, not a double-post."""
        service = _make_service()
        service._get_api.return_value = _make_api(
            note={"type": "Note", "id": 54, "position": None, "web_url": "u"},
        )

        with _patch_position():
            _msg, code = post_comment_impl(service, "org/repo", 7, "nit", file="src/foo.py", line=42)

        assert code == 1
        assert OutboundClaim.objects.filter(
            idempotency_key__startswith="gitlab_note:org/repo!7:",
        ).exists(), "record_note_claim must run on the degraded path to keep a retry idempotent"


class TestInlinePostSucceedsOnAnchoredLine:
    """A genuine inline DiffNote (``position.new_path`` present) returns rc=0."""

    def test_anchored_diffnote_returns_rc0(self) -> None:
        service = _make_service()
        service._get_api.return_value = _make_api(
            note={
                "type": "DiffNote",
                "id": 60,
                "position": {"new_path": "src/foo.py", "new_line": 42},
                "web_url": "https://gitlab.example/org/repo/-/mr/7#note_60",
            },
        )

        with _patch_position():
            msg, code = post_comment_impl(service, "org/repo", 7, "nit", file="src/foo.py", line=42)

        assert code == 0, f"a properly-anchored inline DiffNote must succeed, got rc={code!r} msg={msg!r}"
        assert "discussion_id=disc-1" in msg


class TestInlinePostGenuineFailure:
    """A transport-level POST failure stays rc=1 and records no claim."""

    def test_post_json_none_returns_rc1(self) -> None:
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
