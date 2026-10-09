"""The added lines of one file's unified diff, keyed by their line number in the new file."""

import re

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(diff_text: str) -> dict[int, str]:
    """``{new_line_number: text}`` for every ``+``-added line; anything before the first hunk is ignored."""
    added: dict[int, str] = {}
    number: int | None = None
    for raw in diff_text.splitlines():
        hunk = _HUNK_HEADER.match(raw)
        if hunk:
            number = int(hunk.group(1))
            continue
        if number is None or raw.startswith("-"):
            continue
        if raw.startswith("+"):
            added[number] = raw[1:]
        number += 1
    return added
