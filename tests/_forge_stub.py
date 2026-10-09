"""Shared test-infra helper: the reads every merge-path ``gh`` stub owes.

The substrate detector reads the PR's changed paths at the merge chokepoint and holds
the merge when that list comes back empty — a real open PR always changes at least one
file, so ``[]`` is a failed read, not a proven non-substrate diff. The published-message
gate reads the PR title/body and refuses when that read fails. A stub whose fall-through
returns an empty body therefore turns every merge test into a refusal. Route the
fall-through through :func:`merge_path_stdout` so the rule lives in one place instead of
once per stub.
"""

import json
from pathlib import Path

CLEAN_PR_TITLE = "Tidy the widget"
CLEAN_PR_BODY = "Tidies the widget."

_CHANGED_FILES_ENDPOINT = "/files"
MESSAGE_READ_FIELDS = "title,body"


def merge_path_stdout(joined_argv: str, *, otherwise: str = "") -> str:
    """The changed-files or title/body answer when *joined_argv* is that read, else *otherwise*."""
    if _CHANGED_FILES_ENDPOINT in joined_argv:
        return "README.md\n"
    if MESSAGE_READ_FIELDS in joined_argv:
        return json.dumps({"title": CLEAN_PR_TITLE, "body": CLEAN_PR_BODY})
    return otherwise


def merge_request_payload(argv: list[str]) -> dict[str, object] | None:
    """The JSON body a bound merge sends as ``gh api --input <file>``, readable only during the call."""
    if "--input" not in argv:
        return None
    return json.loads(Path(argv[argv.index("--input") + 1]).read_text(encoding="utf-8"))
