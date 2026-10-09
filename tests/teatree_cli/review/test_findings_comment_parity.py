"""The findings-review checks and ``review post-comment``'s gate chain agree (#4968).

Both compose the predicates in ``teatree.core.review.comment_checks``. This pins the two
COMPOSITIONS by driving the real ``run_pre_publish_gates`` on a colleague's MR with no escapes:
the first gate that refuses is the check the core composition names, and a body one passes the
other passes too — for a general note and for an inline comment next to an author's TODO.
"""

from unittest.mock import patch

import pytest

from teatree.cli.review import ReviewService
from teatree.cli.review.pre_publish_gates import run_pre_publish_gates
from teatree.core.backend_protocols import PrReview, PrReviewComment
from teatree.core.review.comment_checks import review_refusal
from tests.teatree_backends._gitlab_wire import GitLabWire

_MR = "projects/o%2Fr/merge_requests/7"
_DIFF = "@@ -9,0 +10,4 @@\n+def retry():\n+    pass\n+    # TODO: bound the retry loop\n+    return\n"
_TODO_LINE = 12

_CLI_REFUSALS = (
    ("Refusing colleague-MR", "colleague prose cap"),
    ("Refusing bloated review note", "comment bloat"),
    ("Refusing general note", "multi-finding general note"),
    ("Refusing MR-level draft note", "inline drafts pending"),
    ("Refusing TODO-anchored blocker post", "TODO-anchored blocker"),
    ("'missing/wrong/broken'", "unbacked claim"),
)


def _colleague_mr() -> GitLabWire:
    return GitLabWire(
        {
            "user": {"username": "alice"},
            _MR: {"author": {"username": "carol"}},
            f"{_MR}/draft_notes": [],
            f"{_MR}/changes": {"changes": [{"old_path": "a.py", "new_path": "a.py", "diff": _DIFF}]},
        }
    )


def _cli_first_refusing_check(body: str, *, line: int = 0) -> str:
    service = ReviewService("token", repo="o/r", api=_colleague_mr())
    with patch("teatree.cli.review.pre_publish_gates.check_on_behalf", return_value=""):
        refusal = run_pre_publish_gates(
            service,
            repo="o/r",
            mr=7,
            note=body,
            file="a.py" if line else "",
            line=line,
            action="post_comment",
            evidence=None,
        )
    if not refusal:
        return ""
    return next(name for prefix, name in _CLI_REFUSALS if prefix in refusal)


def _core_first_refusing_check(review: PrReview) -> str:
    refusal = review_refusal(review, file_diffs=lambda: {"a.py": _DIFF})
    return refusal.split(" — ", 1)[1].split(":", 1)[0] if refusal else ""


_GENERAL_BODIES = (
    "rename the retry helper",
    "see `a.py:10` and `b.ts`",
    "see `a.py:10` and `b.ts:3`",
    "1. a.py: rename\n2. b.py: guard",
    "@bob flagged this loop",
    "the `@bob` fixture is unused",
    "per the thread at 1717000000.123456",
    "relates to #1234, ping the author",
    "tracked at #1234",
    "the retry helper is missing",
    "a.py:1 is missing a guard and b.py:2 too",
    "a\n\nb\n\nc\n\nd",
    "word " * 201,
    "@bob said so\n\na\n\nb\n\nc",
    "@bob see a.py:1 and b.py:2",
    "a.py:1\n\nb.py:2\n\nc\n\nd",
    "@bob the retry helper is missing",
    "word " * 201 + "and the retry helper is missing",
)

_INLINE_BODIES = (
    "rename the retry helper",
    "this loop must be bounded",
    "the bound must be added, the guard is missing",
    "@bob says this must be fixed",
    "word " * 201 + "so it must be fixed",
    "the retry helper is missing",
    "see `a.py:10` and `b.ts:3`",
)


@pytest.mark.parametrize("body", _GENERAL_BODIES)
def test_a_general_note_is_refused_by_the_same_check_in_both_chains(body: str) -> None:
    core = _core_first_refusing_check(PrReview(commit_sha="c" * 40, body=body, comments=(), marker="<!-- m -->"))
    assert core == _cli_first_refusing_check(body)


@pytest.mark.parametrize("body", _INLINE_BODIES)
def test_an_inline_comment_is_refused_by_the_same_check_in_both_chains(body: str) -> None:
    comment = PrReviewComment(path="a.py", line=_TODO_LINE, body=body)
    core = _core_first_refusing_check(PrReview(commit_sha="c" * 40, body="", comments=(comment,), marker="<!-- m -->"))
    assert core == _cli_first_refusing_check(body, line=_TODO_LINE)
