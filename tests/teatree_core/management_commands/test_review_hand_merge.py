"""``t3 <overlay> review hand-merge`` — record a verdict a reviewer returned as prose.

Every check runs before the first write: a refused envelope leaves no ``ReviewVerdict`` row.
Only the forge head read is faked.
"""

import json
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command

from teatree.agents import review_envelope_recorder
from teatree.core.management.commands import _review_hand_merge
from teatree.core.modelkit.forge_readability import LiveHeadRead
from teatree.core.models import ReviewVerdict
from teatree.core.review import nit_lane

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SLUG = "souliane/teatree"
_PR_ID = 5870
_HEAD = "497d468df76022b280caffceb400739d5ced9baa"
_MOVED_HEAD = "1f2e3d4c5b6a79880011223344556677889900aa"
_LIVE_HEAD_PROBE = "teatree.core.review.verdict_head_binding.live_head_at"


def _envelope(**overrides: object) -> dict[str, object]:
    return {
        "verdict": "merge_safe",
        "reviewed_sha": _HEAD,
        "reviewer_identity": "cold-reviewer-agent",
        "gh_verify_result": "green",
        "blast_class": "logic",
        "findings": [],
        **overrides,
    }


def _prose(envelope: dict[str, object]) -> str:
    return (
        "I read the diff end to end and ran the affected tests.\n\n"
        f"{json.dumps({'review_verdict': envelope}, indent=2)}\n\n"
        "That is my whole verdict; nothing else is outstanding.\n"
    )


def _hand_merge_reply(
    tmp_path: Path, reply: str, *, order_text: str = "merge it", live_head: str = _HEAD
) -> dict[str, object]:
    envelope_file = tmp_path / "reviewer-reply.txt"
    envelope_file.write_text(reply, encoding="utf-8")
    with patch(_LIVE_HEAD_PROBE, return_value=LiveHeadRead.of(live_head)):
        return cast(
            "dict[str, object]",
            call_command(
                "review",
                "hand-merge",
                str(_PR_ID),
                _SLUG,
                envelope_file=str(envelope_file),
                order_text=order_text,
            ),
        )


def _hand_merge(
    tmp_path: Path, envelope: dict[str, object], *, order_text: str = "merge it", live_head: str = _HEAD
) -> dict[str, object]:
    return _hand_merge_reply(tmp_path, _prose(envelope), order_text=order_text, live_head=live_head)


def _verdict_count() -> int:
    return ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).count()


class TestHandMergeRecordsOneVerdict:
    def test_clean_envelope_in_prose_records_exactly_one_verdict_at_the_live_head(self, tmp_path: Path) -> None:
        result = _hand_merge(tmp_path, _envelope())

        assert result["recorded"] is True
        recorded = ReviewVerdict.objects.get(slug=_SLUG, pr_id=_PR_ID)
        assert recorded.reviewed_sha == _HEAD
        assert recorded.verdict == ReviewVerdict.Verdict.MERGE_SAFE

    def test_an_abbreviated_asserted_sha_binds_to_the_full_live_head(self, tmp_path: Path) -> None:
        _hand_merge(tmp_path, _envelope(reviewed_sha=_HEAD[:12]))

        assert ReviewVerdict.objects.get(slug=_SLUG, pr_id=_PR_ID).reviewed_sha == _HEAD

    def test_an_order_citing_the_live_sha_and_pr_is_accepted(self, tmp_path: Path) -> None:
        order = f"merge PR #{_PR_ID} at {_HEAD[:10]} once green"

        result = _hand_merge(tmp_path, _envelope(), order_text=order)

        assert result["recorded"] is True

    @pytest.mark.parametrize("qualifier", ["issue", "ticket", "ticket issue", "Issue"])
    def test_an_order_naming_its_ticket_issue_is_accepted(self, tmp_path: Path, qualifier: str) -> None:
        order = f"merge PR #{_PR_ID} at {_HEAD[:10]} ({qualifier} #5000)"

        result = _hand_merge(tmp_path, _envelope(), order_text=order)

        assert result["recorded"] is True


class TestHandMergeRefusesBeforeAnyWrite:
    def test_a_rejected_nit_refuses_with_no_verdict_row(self, tmp_path: Path) -> None:
        nit = {
            "severity": "nit",
            "kind": "nit",
            "summary": "ask @alice to rename this helper",
            "file": "a.py",
            "line": 2,
        }

        result = _hand_merge(tmp_path, _envelope(findings=[nit]))

        assert "comment check" in str(result["error"])
        assert _verdict_count() == 0

    @pytest.mark.parametrize(
        "overrides",
        [
            {"verdict": "ship_it"},
            {"gh_verify_result": "purple"},
            {"blast_class": "cosmic"},
            {"evidence_gaps": ["mood"]},
            {"findings": [{"severity": "blocking", "summary": "x", "file": "a.py", "line": 1, "kind": "fatal"}]},
        ],
    )
    def test_an_invalid_enum_refuses_with_no_verdict_row(self, tmp_path: Path, overrides: dict[str, object]) -> None:
        result = _hand_merge(tmp_path, _envelope(**overrides))

        assert result["error"]
        assert _verdict_count() == 0

    def test_an_unknown_key_refuses_with_no_verdict_row(self, tmp_path: Path) -> None:
        result = _hand_merge(tmp_path, _envelope(approved_by="me"))

        assert "approved_by" in str(result["error"])
        assert _verdict_count() == 0

    def test_prose_without_an_envelope_refuses_with_no_verdict_row(self, tmp_path: Path) -> None:
        result = _hand_merge_reply(tmp_path, "Looks good to me, ship it.\n")

        assert "review_verdict" in str(result["error"])
        assert _verdict_count() == 0

    def test_a_moved_head_refuses_with_no_verdict_row(self, tmp_path: Path) -> None:
        result = _hand_merge(tmp_path, _envelope(), live_head=_MOVED_HEAD)

        assert _MOVED_HEAD[:8] in str(result["error"])
        assert _verdict_count() == 0

    @pytest.mark.parametrize(
        ("order_text", "cited"),
        [
            (f"merge at {_MOVED_HEAD[:10]}", _MOVED_HEAD[:10]),
            ("merge PR #5999 now", "#5999"),
        ],
    )
    def test_a_stale_order_refuses_with_no_verdict_row(self, tmp_path: Path, order_text: str, cited: str) -> None:
        result = _hand_merge(tmp_path, _envelope(), order_text=order_text)

        assert "STALE_ORDER" in str(result["error"])
        assert cited in str(result["error"])
        assert _verdict_count() == 0

    def test_a_stale_pr_is_named_without_the_issue_it_was_cited_beside(self, tmp_path: Path) -> None:
        result = _hand_merge(tmp_path, _envelope(), order_text="merge PR #5999 for issue #5000")

        assert "#5999" in str(result["error"])
        assert "#5000" not in str(result["error"])


class TestOneNitCheckServesBothCallers:
    def test_the_recorder_and_hand_merge_call_the_same_public_function(self) -> None:
        assert review_envelope_recorder.nit_comment_error is nit_lane.nit_comment_error
        assert _review_hand_merge.nit_comment_error is nit_lane.nit_comment_error
