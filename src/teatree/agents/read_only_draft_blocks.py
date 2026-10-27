"""The ``PHASE:`` blocks of the draft-returning phases in the read-only executor: it holds no forge, chat or ``t3``."""

REQUESTING_REVIEW_LINES: tuple[str, ...] = (
    "",
    "PHASE: requesting_review",
    "Decide from the PULL REQUESTS block in the task message whether one pull request is ready for human review,",
    "and draft its review request. You cannot send it: the worker sends your draft through the review-request",
    "gates (live dedup, recorded approval, draft and metadata checks), so run no command and call no tool to post.",
    "RETURN YOUR DRAFT in the final JSON result:",
    '  "review_request": {"ready": true|false, "pr_url": "<one URL from the PULL REQUESTS block>",',
    '                     "title": "<type(scope): description>", "reasons": ["<why not ready>"]}',
    "`ready` is true only when CI is green at the head and the metadata check passes.",
    "`ready` false sends nothing and fails the task with your `reasons`; a result with no `review_request` is refused.",
    "`pr_url` only selects among the ticket's own pull requests; the worker reads the head and the ticket itself.",
)

SHIPPING_LINES: tuple[str, ...] = (
    "",
    "PHASE: shipping - readiness verdict",
    "You cannot push or open a pull request. The worker's ship job does both, after its own ship gates, when you",
    "return ready. Judge the SHIP STATE block in the task message and the checkout you can read:",
    "ready only when no phase is still needed, no self-review hold is open, every worktree has commits ahead",
    "of its base and no uncommitted tracked change, and the diff holds what the ticket asked for.",
    "RETURN YOUR VERDICT in the final JSON result:",
    '  "ship_readiness": {"ready": true|false, "reasons": ["<blocker>"]}',
    "`ready` false ships nothing and fails the task with your `reasons`; a result with no `ship_readiness` is refused.",
)
