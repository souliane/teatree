"""A gate's remediation text must name a command the CLI actually resolves.

A wrong remediation converts a correct block into an apparent dead end, and the caller's
next move is an override token. `t3 <overlay> review post-comment` shipped for months and
resolves nowhere — the review create/edit/delete seam is top-level `t3 review`.
"""

import re

import pytest

from teatree.hooks.raw_review_post_detect import ISSUE_NOTE_DENY_REASON, MR_REVIEW_DENY_REASON

_T3_COMMAND = re.compile(r"`t3 ([^`]+)`")

# `t3 <overlay> ...` is a real shape — the overlay groups own ticket/workspace/e2e verbs.
# It is wrong only for `review`, whose create/edit/delete commands are top-level.
_OVERLAY_SCOPED_REVIEW = re.compile(r"t3 <overlay> review\b")


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("MR_REVIEW_DENY_REASON", MR_REVIEW_DENY_REASON),
        ("ISSUE_NOTE_DENY_REASON", ISSUE_NOTE_DENY_REASON),
    ],
)
class TestADenyReasonPointsSomewhereReal:
    def test_it_never_scopes_a_review_command_to_an_overlay(self, name: str, reason: str) -> None:
        assert not _OVERLAY_SCOPED_REVIEW.search(reason), (
            f"{name} names `t3 <overlay> review …`, which resolves nowhere — the overlay"
            " groups expose only record/status/lock verbs. Use top-level `t3 review …`."
        )

    def test_it_names_at_least_one_command(self, name: str, reason: str) -> None:
        assert _T3_COMMAND.search(reason), f"{name} blocks without naming a `t3` command to use instead"


class TestTheMrReasonNamesTheScannablePath:
    def test_it_names_body_file(self) -> None:
        assert "--body-file" in MR_REVIEW_DENY_REASON, (
            "a body passed inline or on stdin is refused as unscannable by the"
            " banned-terms gate, so the remediation has to name --body-file"
        )
