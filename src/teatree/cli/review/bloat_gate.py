"""Review-comment bloat gate (souliane/teatree#2663).

"Comment bloat" on colleague-MR/PR reviews recurred repeatedly: review
notes that drag in *project chatter* — coordinating with people, naming
stakeholders, quoting standup — instead of stating the finding on the
diff. The user flagged "too many comments" on two customer MRs. The
length dimension of bloat is already owned by the colleague-MR shape gate
(:mod:`teatree.cli.review.shape_gate`, a 3-paragraph / 200-word cap), so
this gate does NOT add a parallel length cap. It closes the orthogonal
gap that gate does not cover: **off-topic project chatter** in a review
note.

A review comment is about the DIFF, not the tracker. The bloat shapes
this gate refuses:

* **Stakeholder ``@handle``** — naming a person to coordinate with
    (``@bob said in standup``). No legitimate use in a diff-anchored
    review note. An ``@tag`` inside a code span (backticks / a fenced
    block) or from the Gherkin tag vocabulary (``@ready``, ``@blocked``,
    ``@awaiting-merge``, …) is code the finding quotes, not a person.
* **Slack timestamp** — quoting a Slack thread by ``ts`` (a 10.6-digit
    Unix timestamp). Pure project-chatter.
* **Tracker reference + coordination directive** — a ticket/PR id
    (``#1234`` / ``!42``) paired with social-coordination language
    (``ping the author``, ``sync with the team``, ``discuss in
    standup``). A *bare* ``tracked at #1234`` pointer is NOT bloat — it
    is the legitimate non-blocker form the TODO gate
    (:mod:`teatree.cli.review.todo_gate`) steers reviewers toward — so a
    tracker id alone passes; only the id-plus-chatter combination is the
    "this comment is project coordination, not code review" shape.

The gate runs on the same ``_run_pre_publish_gates`` chain as its
siblings and returns a non-empty steering string to refuse (the caller
short-circuits the GitLab API call with ``(message, 1)``), or ``""`` to
proceed. ``allow_bloat`` is the documented per-call escape for a
genuinely load-bearing reference, surfaced on the CLI as ``--allow-bloat``
and mirroring the sibling ``--allow-long-review`` / ``--force-general``
overrides. It never blocks the agent's own self-rescue: it inspects only
the comment body, never the network.

Sibling gates on the same chain:

* :mod:`teatree.cli.review.shape_gate` — colleague-MR prose-size cap (the
    length dimension this gate deliberately leaves alone).
* :mod:`teatree.cli.review.general_inline_gate` — multi-finding general note.
* :mod:`teatree.cli.review.todo_gate` — author-marked TODO anchor.

This gate is independent of all three and forge-neutral.
"""

from teatree.core.review.comment_checks import references_project_chatter


def check_review_bloat(*, body: str, allow_bloat: bool = False) -> str:
    """Return a non-empty steering error when ``body`` carries project chatter.

    Returns ``""`` (proceed) when any of these hold:

    * ``allow_bloat`` is set — the documented escape for a genuinely
        load-bearing reference (CLI ``--allow-bloat``), OR
    * the body is empty, OR
    * the body references no project chatter (see
        :func:`~teatree.core.review.comment_checks.references_project_chatter`).

    Otherwise returns a steering error naming the diff-only rule so the
    agent knows what to drop. The caller short-circuits the GitLab API call
    with ``(message, 1)`` — the same shape the sibling gates use.
    """
    if allow_bloat or not body:
        return ""
    if references_project_chatter(body):
        return _chatter_error()
    return ""


def _chatter_error() -> str:
    """Build the project-chatter refusal naming the diff-only rule."""
    return (
        "Refusing bloated review note: it carries project chatter (an `@handle` "
        "stakeholder, a Slack timestamp, or a tracker id like `#1234`/`!42` paired with "
        "a coordinate-with-people directive). A review comment is about the DIFF, not the "
        "project tracker — drop the coordination and state the finding on the code itself. "
        "A bare `tracked at #1234` pointer is fine; the social directive (`ping the "
        "author`, `sync with the team`, `in standup`) is the bloat. Pass --allow-bloat to "
        "override ONLY when the reference is genuinely load-bearing for the finding."
    )
