r"""Resolve a unified diff to the skill SECTIONS it touched (#3944).

A clean-room scenario's graded system prompt is not its YAML — it is
``agent_path`` narrowed to ``agent_sections`` (see
:func:`teatree.eval.context_budget.extract_sections`). So the selective-PR lane
cannot decide "did this PR move what that scenario grades?" from changed file
PATHS alone: every scenario on ``skills/rules/SKILL.md`` would answer the same
way for any edit to any of its ~70 sections. This module supplies the missing
granularity — ``path -> the section titles the diff touched`` — which
:func:`teatree.eval.changed_scenarios.selection_for_changed` intersects with
each spec's declared ``agent_sections``.

Attribution mirrors ``extract_sections`` exactly, so a section is judged changed
on the same span the grader would have SENT: a heading's section runs to the next
SAME-OR-SHALLOWER heading, so an edit inside a ``### `` carve-out is attributed to
that carve-out AND to the ``## `` rule containing it. New-file line numbers come
from the hunk headers and are mapped against the post-image on disk, so the diff
must be taken against the checked-out tree (what the CI lane does).

FAIL-SAFE DIRECTION. Every uncertainty omits the path from the mapping, which the
selector reads as "granularity unknown" and answers with EVERY scenario grading
that file. A deleted file, an unreadable post-image, a file with no ``## ``
headings at all, a preamble-only edit (the preamble is prepended to every
section-scoped prompt, so no section is unaffected) — all degrade to selecting
MORE, never fewer. Over-selection costs API budget; under-selection is the silent
green #3944 reports, where the lane passes having proven nothing.
"""

import re
from bisect import bisect_left
from pathlib import Path

from teatree.eval.context_budget import HEADING_RE

#: ``@@ -<old>[,<n>] +<new>[,<n>] @@`` — only the NEW-file side is read; the mapping
#: is against the post-image. An omitted count means 1 (git's shorthand); an explicit
#: ``0`` is a pure deletion, whose ``<new>`` is the line PRECEDING the removed block.
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

_DEV_NULL = "/dev/null"


def changed_sections_by_path(diff: str, *, repo_root: Path) -> dict[str, frozenset[str]]:
    """Map each changed path to the ``## `` section titles the diff touched.

    *diff* is unified-diff text (``git diff --unified=0 <base>...HEAD``); zero context
    gives the tightest attribution, and any context lines only ever widen it. *repo_root*
    is what the diff's paths are relative to and where the post-image is read from.

    A path is present ONLY when a non-empty section set was resolved for it — see the
    module docstring's fail-safe rule.
    """
    lines_by_path, headings_by_path = _scan(diff)
    resolved: dict[str, frozenset[str]] = {}
    for path, lines in lines_by_path.items():
        text = _read_post_image(repo_root / path)
        if text is None:
            continue
        sections = _sections_for_lines(text, lines) | headings_by_path.get(path, set())
        if sections:
            resolved[path] = frozenset(sections)
    return resolved


def _scan(diff: str) -> tuple[dict[str, set[int]], dict[str, set[str]]]:
    """Per path: the new-file line numbers the hunks touch, and headings named in hunk bodies.

    ``in_hunk`` is load-bearing: ``+++ b/x`` is a per-file header only BEFORE the first
    ``@@``; the identical bytes inside a hunk body are added content, and treating them as
    a header would retarget the rest of the diff at a file the PR never changed. A
    ``--- ``/``+++ `` PAIR always opens a file entry, so plain ``diff -u`` output (no
    ``diff --git`` line to reset on) parses too.
    """
    lines_by_path: dict[str, set[int]] = {}
    headings_by_path: dict[str, set[str]] = {}
    path: str | None = None
    in_hunk = False
    body = diff.splitlines()
    for index, line in enumerate(body):
        if line.startswith("diff --git "):
            path, in_hunk = None, False
        elif line.startswith("--- ") and index + 1 < len(body) and body[index + 1].startswith("+++ "):
            in_hunk = False
        elif not in_hunk and line.startswith("+++ "):
            path = _header_path(line[4:])
        elif line.startswith("@@"):
            in_hunk = True
            _record_hunk(line, path, lines_by_path)
        elif in_hunk and path is not None and line[:1] in {"+", "-"}:
            _record_heading(line[1:], path, headings_by_path)
    return lines_by_path, headings_by_path


def _record_hunk(line: str, path: str | None, lines_by_path: dict[str, set[int]]) -> None:
    """Record the hunk's new-file lines, or nothing when the header is not a plain two-way one.

    A combined (merge) header — ``@@@ -a,b -c,d +e,f @@@`` — does not match, so it records
    nothing and the path falls through to the fail-safe whole-file reading.
    """
    match = _HUNK_RE.match(line)
    if path is None or match is None:
        return
    lines_by_path.setdefault(path, set()).update(_new_lines(match))


def _record_heading(text: str, path: str, headings_by_path: dict[str, set[str]]) -> None:
    heading = HEADING_RE.match(text)
    if heading is not None:
        headings_by_path.setdefault(path, set()).add(heading.group(2))


def _header_path(token: str) -> str | None:
    stripped = token.strip()
    if stripped == _DEV_NULL:
        return None
    return stripped.removeprefix("b/")


def _new_lines(match: re.Match[str]) -> range:
    start = int(match.group(1))
    count = 1 if match.group(2) is None else int(match.group(2))
    if count == 0:
        # A pure deletion has no post-image line of its own; attribute it to the line it
        # was removed after, so the enclosing section is still named.
        return range(max(start, 1), max(start, 1) + 1)
    return range(start, start + count)


def _read_post_image(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _sections_for_lines(text: str, lines: set[int]) -> set[str]:
    headings = [
        (number, len(match.group(1)), match.group(2))
        for number, raw in enumerate(text.splitlines(), start=1)
        if (match := HEADING_RE.match(raw))
    ]
    if not headings:
        return set()
    ordered = sorted(lines)
    if ordered[0] < headings[0][0]:
        # A preamble edit changes the framing every section-scoped prompt carries, so no
        # section is unaffected — answer "unknown" rather than a subset that is a lie.
        return set()
    touched: set[str] = set()
    total = len(text.splitlines())
    for index, (number, depth, title) in enumerate(headings):
        end = next((later[0] for later in headings[index + 1 :] if later[1] <= depth), total + 1)
        position = bisect_left(ordered, number)
        if position < len(ordered) and ordered[position] < end:
            touched.add(title)
    return touched


__all__ = ["changed_sections_by_path"]
