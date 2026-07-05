"""Canonical ``FEATURE`` vs ``FIX`` classification for every ticket-intake site (#17).

``Ticket.Kind.FIX`` gates two downstream consumers — the S2 defect-escape signal
(:func:`teatree.core.factory_signal_queries.compute_s2`) and the fix-record
Definition-of-Done merge gate (:mod:`teatree.core.gates.fix_dod_gate`) — yet
before #17 no production path ever *wrote* it, so S2 read a vacuous FEATURE-only
world and the DoD gate was a permanent no-op contradicting BLUEPRINT.md.

This module is the single writer. Every site that creates a ticket routes its kind
decision through :func:`classify_ticket_kind`, so the classification can never
diverge across sites:

* ``teatree.core.resolve`` — auto-registering a manually-added git worktree.
* ``teatree.core.management.commands._workspace_ticket_intake`` — ``workspace ticket``.
* ``teatree.core.management.commands.tasks`` — ``tasks create --kind``.
* ``teatree.loop.persistence`` — correction-zone handlers (red-card/red-MR-fix/e2e-fix/skill-drift) are FIX by origin.
"""

import enum
import re
from collections.abc import Iterable

from teatree.core.models.ticket import Ticket


class TicketOrigin(enum.StrEnum):
    """The provenance of a ticket-creation call, for kind classification.

    A ``CORRECTION`` flow (red-card / red-MR-fix / e2e-fix / skill-drift) is a
    fix by construction — the ticket exists *because* something broke — so it
    classifies FIX regardless of labels/title. ``USER`` intake defers to the
    label and title signals.
    """

    USER = "user"
    CORRECTION = "correction"


# A curated tracker label names a defect when a WHOLE segment-token of it — or
# the separator-stripped whole label — equals one of these (see _labels_signal_fix
# for why matching is token-boundary, never substring).
_FIX_LABEL_KEYWORDS: frozenset[str] = frozenset(
    {"bug", "bugfix", "fix", "fixup", "hotfix", "regression", "defect", "redcard"},
)

# A title/branch classifies FIX only when its first word-token is a conventional
# fix prefix. Conservative on purpose: a mis-classified feature would wedge the
# fix-record DoD gate, so "Add a fix button" (leading "add") stays FEATURE while
# "fix: crash", "hotfix login", and the "123-fix-foo" branch shape are FIX.
_FIX_TITLE_PREFIXES: frozenset[str] = frozenset(
    {"fix", "fixup", "bug", "bugfix", "hotfix", "regression"},
)
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _labels_signal_fix(labels: Iterable[str]) -> bool:
    # Token/segment-boundary match, never substring: a curated label is split on
    # its separators (":", "/", "-", "_", space) and a WHOLE token — or the
    # separator-stripped whole label — must equal a defect keyword. Substring
    # matching misfires ("debug" ⊃ "bug", "prefix"/"suffix" ⊃ "fix", "defective"
    # ⊃ "defect"), flipping a feature to FIX and wedging the DoD gate; this keeps
    # the label path as conservative as the title path.
    for label in labels:
        lowered = label.lower()
        if any(token in _FIX_LABEL_KEYWORDS for token in _TOKEN_SPLIT.split(lowered)):
            return True
        if _TOKEN_SPLIT.sub("", lowered) in _FIX_LABEL_KEYWORDS:
            return True
    return False


def _title_signals_fix(title: str) -> bool:
    tokens = [token for token in _TOKEN_SPLIT.split(title.strip().lower()) if token]
    # Skip a leading ``<number>`` so a ``<number>-<slug>`` branch classifies on
    # its slug (``123-fix-foo`` → first non-numeric token ``fix``).
    first = next((token for token in tokens if not token.isdigit()), "")
    return first in _FIX_TITLE_PREFIXES


def parse_kind(value: str) -> Ticket.Kind:
    """Coerce a ``--kind`` CLI value to a :class:`Ticket.Kind`; raise on an unknown one."""
    normalized = value.strip().lower()
    for kind in Ticket.Kind:
        if kind.value == normalized:
            return kind
    valid = ", ".join(kind.value for kind in Ticket.Kind)
    msg = f"unknown ticket kind {value!r}; expected one of: {valid}"
    raise ValueError(msg)


def classify_ticket_kind(
    *,
    labels: Iterable[str] = (),
    title: str = "",
    origin: str = TicketOrigin.USER,
    explicit: str = "",
) -> Ticket.Kind:
    """Return the canonical ``FEATURE`` / ``FIX`` classification (first match wins).

    1. ``explicit`` — an operator's ``--kind`` value overrides every inference.
    2. ``origin`` is a correction flow → ``FIX``.
    3. any ``label`` names a defect (bug / fix / hotfix / regression / red-card) → ``FIX``.
    4. ``title`` leads with a conventional fix prefix (``fix:`` / ``hotfix`` …) → ``FIX``.
    5. otherwise ``FEATURE`` — the safe default; inference stays conservative
        because a mis-classified feature would wedge the fix-record DoD gate.
    """
    if explicit.strip():
        return parse_kind(explicit)
    if origin == TicketOrigin.CORRECTION:
        return Ticket.Kind.FIX
    if _labels_signal_fix(labels) or _title_signals_fix(title):
        return Ticket.Kind.FIX
    return Ticket.Kind.FEATURE
