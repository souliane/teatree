"""Parse a ``Closes/Fixes/Resolves #N`` footer out of a PR/MR body.

The foundation-layer home for the close-keyword parser so both the
orchestration-layer tick reconciler (:mod:`teatree.loop.pr_ticket_index`,
:mod:`teatree.loop.manual_pr_reconcile`) and the domain-layer forge-authoritative
PR-budget backstop (:mod:`teatree.core.gates.pr_budget_forge`) resolve a PR to its
ticket the same way. It cannot live in ``teatree.loop`` because ``teatree.core``
(domain) may not import ``teatree.loop`` (orchestration); ``teatree.utils`` is the
shared floor both layers already depend on.
"""

import re

# Matches ``Closes #123`` / ``Fixes: #456`` / ``Resolves #789`` (and the
# plural/past-tense variants the platforms recognise — ``closed``, ``fixed``,
# ``resolved``). Broader than the close-keyword set the ship pipeline emits
# because the parser must accept anything GitHub/GitLab auto-link. Anchored at
# a word boundary so ``preCloses#`` doesn't match.
CLOSE_KEYWORD_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[\s:]*#(\d+)",
    re.IGNORECASE,
)


def parse_closes_ticket(description: str) -> str:
    """Return the first ``#N`` after a Closes/Fixes/Resolves keyword, else ``""``."""
    match = CLOSE_KEYWORD_RE.search(description)
    return match.group(1) if match else ""
