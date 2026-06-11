"""Actionable refusal messages for the ``ticket transition`` direct-CLI driver.

Factored out of the ``ticket`` command so the (cap-bound) ``ticket.py`` god-module
keeps shrinking. These mirror FSM conditions the workflow / lifecycle paths also
consult, so the direct-CLI driver surfaces a fix-it message instead of a generic
``not allowed``.
"""

from teatree.core.models import Ticket


def review_context_refusal(ticket: Ticket, transition_name: str) -> str:
    """Actionable refusal when a `review` verdict lacks recorded context, else ``""``.

    Mirrors the ``review_context_satisfied`` FSM condition the workflow path
    (``Task.complete()``) and the lifecycle ``reviewing``-phase path also consult,
    so the direct-CLI review driver gets a fix-it message instead of a generic
    "not allowed".
    """
    if transition_name != "review" or ticket.review_context_satisfied():
        return ""
    return (
        f"Transition 'review' refused: require_review_context is on but no referenced-context "
        f"retrieval is recorded for ticket {ticket.pk}. Fetch the work item from its source, follow "
        f"its links, download + analyze the referenced documents, then `t3 <overlay> lifecycle "
        f"record-review-context {ticket.pk} --work-item <url> --documents <urls> "
        f"--analysis <how-checked>` and retry."
    )
