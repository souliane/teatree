"""General-note carrying inline findings gate (souliane/teatree#72, round 2).

The posting command validates ``--file`` and ``--line`` together so a partially
specified inline anchor cannot silently become a general note.

This module closes the *other half* of the same discipline: two distinct
per-line findings crammed into ONE general note instead of posted one per
line. A general post without an inline anchor therefore needs its own check.

The gate runs only on the **general** path of a publishing method (no
``file``+``line`` anchor) and refuses the post — before any GitLab call —
when the body looks like a multi-point per-line review, i.e. it either:

* references **2+ distinct ``path.ext:line`` locations** (a concrete
    file:line cite for each finding), OR
* reads as a **numbered finding list where 2+ items each name a file**
    (``1. foo.py: ...`` / ``2. bar.ts:14 ...``).

Both shapes say "these are N inline findings". The remediation steers the
agent to post each one inline with
``t3 review post-comment <repo> <mr> "<note>" --file <path> --line <n>``
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

from teatree.core.review.comment_checks import inline_findings_count, looks_like_inline_findings


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
    return _refusal(inline_findings_count(body))


def _refusal(count: int) -> str:
    """Build the actionable refusal naming the finding count and the inline command."""
    return (
        f"Refusing general note: this looks like {count} inline findings (it references "
        f"{count}+ distinct file:line locations or a numbered per-file finding list). "
        "Post them INLINE — one per finding — with:\n"
        '  t3 review post-comment <repo> <mr> "<note>" --file <path> --line <n>\n'
        "Cramming distinct per-line findings into one general note is the shape "
        "the #72 discipline guards against. Pass --force-general to override ONLY for a "
        "genuinely MR-wide note (a verdict-only summary with no per-line findings)."
    )
