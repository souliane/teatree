"""The findings-comment checks and ``review post-comment``'s gate chain agree (#4968).

Both compose the same predicates in ``teatree.core.review.comment_checks``. This pins the two
COMPOSITIONS: for a general note on a colleague's PR with no escapes, the first CLI gate that
refuses is the check the core composition names, and a body one passes the other passes too.
"""

from typing import Any, cast

import pytest

from teatree.cli.review.bloat_gate import check_review_bloat
from teatree.cli.review.evidence_gate import check_finding_evidence
from teatree.cli.review.general_inline_gate import check_general_inline_findings
from teatree.cli.review.shape_gate import check_review_shape
from teatree.core.review.comment_checks import findings_comment_refusal


class _ColleagueApi:
    def get_json(self, endpoint: str) -> dict[str, object]:
        _ = endpoint
        return {"author": {"username": "carol"}}

    def current_username(self) -> str:
        return "alice"


def _cli_first_refusing_check(body: str) -> str:
    api = cast("Any", _ColleagueApi())
    chain = (
        ("colleague prose cap", check_review_shape(api=api, encoded_repo="o%2Fr", mr=7, body=body, inline=False)),
        ("comment bloat", check_review_bloat(body=body)),
        ("multi-finding general note", check_general_inline_findings(body=body, inline=False)),
        ("unbacked claim", check_finding_evidence(body=body, evidence=None)),
    )
    return next((name for name, refusal in chain if refusal), "")


_BODIES = (
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
)


@pytest.mark.parametrize("body", _BODIES)
def test_the_core_composition_refuses_where_the_cli_chain_refuses(body: str) -> None:
    core = findings_comment_refusal(body, is_own_pr=lambda: False)
    assert (core.split(":", 1)[0] if core else "") == _cli_first_refusing_check(body)
