"""General-note carrying inline findings gate (souliane/teatree#72, round 2).

The #72 fix (:func:`teatree.cli.review.drafts.validate_inline_or_general`)
closed the *half-specified* foot-gun: a ``post-draft-note`` that named a
``--file`` without a ``--line`` (or vice versa) used to silently degrade
into a general (MR-wide) note. That validator stops the degradation at the
typer-wrapper boundary.

This module closes the *other half* of the same discipline, observed on
!6281: under time pressure a reviewer crammed two distinct per-line
findings into ONE general note instead of posting each one inline. The
#72 validator never fired (the call WAS a deliberate ``--general`` post),
so the multi-finding general note went through.

The gate runs only on the **general** path of a publishing method (no
``file``+``line`` anchor) and refuses the post — before any GitLab call —
when the body looks like a multi-point per-line review, i.e. it either:

* references **2+ distinct ``path.ext:line`` locations** (a concrete
    file:line cite for each finding), OR
* reads as a **numbered finding list where 2+ items each name a file**
    (``1. foo.py: ...`` / ``2. bar.ts:14 ...``).

Both shapes say "these are N inline findings". The remediation steers the
agent to post each one inline with
``t3 review post-draft-note <repo> <mr> "<note>" --file <path> --line <n>``
(one per finding), and offers the documented per-call escape
``--force-general`` for a genuinely MR-wide note (a verdict-only summary
with no per-line findings) — mirroring the sibling ``--allow-long-review``
/ ``--allow-todo-blocker`` overrides on the same flow.

Sibling gates on the same ``_run_pre_publish_gates`` chain:

* :mod:`teatree.cli.review.shape_gate` — colleague-MR prose-size cap.
* :mod:`teatree.cli.review.todo_gate` — author-marked TODO anchor.

This gate is independent of both. It is forge-neutral: it inspects only
the comment body and the inline-anchor flag, never the network.
"""

import re

# A ``path.ext:line`` location cite: a dotted file path (so a bare ``foo``
# or a sentence word does not match — the ``.ext`` is required) followed by
# ``:<digits>``. The path segment allows directory separators, dashes,
# underscores, and dots (``a/b-c_d.py``); the extension is 1-8 letters so
# ``module.py:10`` matches but ``ratio 3:2`` or ``12:30`` (a time) does not.
_FILE_LINE_RE = re.compile(
    r"\b[\w./-]+\.[A-Za-z]{1,8}:\d+\b",
)

# A numbered-list item that names a file: ``1. foo.py`` / ``2) bar/baz.ts``.
# The leading marker is ``<digits>`` followed by ``.`` or ``)`` at a line
# start (optionally indented); the item text must contain a dotted file
# path token somewhere on that line.
_NUMBERED_ITEM_RE = re.compile(
    r"^\s*\d+[.)]\s+.*\b[\w./-]+\.[A-Za-z]{1,8}\b",
    re.MULTILINE,
)

MIN_DISTINCT_FINDINGS = 2


def _distinct_file_line_cites(body: str) -> set[str]:
    """Return the set of distinct ``path.ext:line`` location cites in ``body``.

    Distinct-by-text: ``foo.py:10`` and ``foo.py:10`` collapse to one, but
    ``foo.py:10`` and ``foo.py:42`` (two findings in the same file) and
    ``foo.py:10`` and ``bar.ts:3`` (two files) each count as two — the
    multi-finding signal is "more than one place the reviewer is pointing
    at", whether same file or not.
    """
    return {m.group(0) for m in _FILE_LINE_RE.finditer(body)}


def _numbered_file_items(body: str) -> int:
    """Count numbered-list items (``1.`` / ``2)``) that each name a file.

    A numbered list where each item cites a file is the other shape of a
    multi-point per-line review even when the items omit explicit line
    numbers (``1. foo.py: rename …`` / ``2. bar.py: guard …``).
    """
    return sum(1 for _ in _NUMBERED_ITEM_RE.finditer(body))


def looks_like_inline_findings(body: str) -> bool:
    """Whether ``body`` reads as 2+ inline findings crammed into one note.

    True when EITHER the body references
    :data:`MIN_DISTINCT_FINDINGS`+ distinct ``path.ext:line`` locations OR
    a numbered finding list has :data:`MIN_DISTINCT_FINDINGS`+ items that
    each name a file. Both are the "this should have been N inline notes"
    shape (!6281).
    """
    if not body:
        return False
    if len(_distinct_file_line_cites(body)) >= MIN_DISTINCT_FINDINGS:
        return True
    return _numbered_file_items(body) >= MIN_DISTINCT_FINDINGS


def check_general_inline_findings(*, body: str, inline: bool, force_general: bool = False) -> str:
    """Return a non-empty refusal when a GENERAL note carries multiple inline findings.

    Returns ``""`` (proceed) when any of these hold:

    * ``force_general`` is set — the documented per-call escape for a
        genuinely MR-wide note (verdict-only, no per-line findings),
        surfaced on the CLI as ``--force-general`` and mirroring the
        sibling ``--allow-long-review`` / ``--allow-todo-blocker`` overrides.
    * ``inline`` is true — the post IS being anchored inline
        (``--file``/``--line`` supplied), so it is not the general-note
        cramming this gate targets. The #72 validator already governs the
        inline-vs-general split.
    * ``body`` does not look like 2+ inline findings.

    Otherwise returns a clear refusal naming the finding count and the
    inline per-finding command the agent should use instead. The caller
    short-circuits the GitLab API call with ``(message, 1)`` — the same
    shape the sibling gates use.
    """
    if force_general or inline:
        return ""
    if not looks_like_inline_findings(body):
        return ""
    cites = _distinct_file_line_cites(body)
    numbered = _numbered_file_items(body)
    count = max(len(cites), numbered)
    return _refusal(count)


def _refusal(count: int) -> str:
    """Build the actionable refusal naming the finding count and the inline command."""
    return (
        f"Refusing general note: this looks like {count} inline findings (it references "
        f"{count}+ distinct file:line locations or a numbered per-file finding list). "
        "Post them INLINE — one per finding — with:\n"
        '  t3 review post-draft-note <repo> <mr> "<note>" --file <path> --line <n>\n'
        "Cramming distinct per-line findings into one general note is the !6281 shape "
        "the #72 discipline guards against. Pass --force-general to override ONLY for a "
        "genuinely MR-wide note (a verdict-only summary with no per-line findings)."
    )
