"""Leaf constants + predicates shared across the dream member-weight ladder (#2545, F6.11).

The engine (:mod:`teatree.loops.dream.engine`) ranks replay members and the merge
phase (:mod:`teatree.loops.dream.merge`) picks a survivor by the SAME kind-aware
weight ladder, and three modules (engine / merge / decay) each re-derived the
"does this text carry BINDING doctrine?" heuristic. Both were copied per module, so
a change to the ladder or the binding rule had to be mirrored in three places or
they drifted. This leaf holds the single copy of each.

Imports NOTHING from the ``dream`` package (stdlib only), so engine / merge / decay
depend on it without any risk of an import cycle.
"""

from typing import Final

#: Weight floors per member, highest signal first — the ladder the engine ranks
#: replay members by and the merge phase orders survivors by. Kept here so the two
#: consumers can never drift. The floors are KIND-AWARE at the call site: the engine
#: reserves the ``BINDING`` / ``feedback_`` floors for curated memory members so a
#: transcript that merely QUOTES doctrine cannot outrank the memory that owns it.
WEIGHT_BINDING: Final = 100
WEIGHT_FEEDBACK: Final = 90
WEIGHT_CORRECTION: Final = 80
WEIGHT_RETRO: Final = 70
WEIGHT_COLD_REVIEW: Final = 50
WEIGHT_DENY_STREAK: Final = 40
WEIGHT_OTHER: Final = 10


def is_binding_text(text: str) -> bool:
    """True when *text* carries BINDING / Non-Negotiable doctrine.

    The single copy of the binding heuristic the engine weight, the merge survivor /
    conflict decision, and the decay signal score all read, so the three can never
    disagree on what counts as binding. Matches either the ``binding`` marker or a
    ``Non-Negotiable`` clause (both mark load-bearing user doctrine).
    """
    lowered = text.lower()
    return "binding" in lowered or "non-negotiable" in lowered


__all__ = [
    "WEIGHT_BINDING",
    "WEIGHT_COLD_REVIEW",
    "WEIGHT_CORRECTION",
    "WEIGHT_DENY_STREAK",
    "WEIGHT_FEEDBACK",
    "WEIGHT_OTHER",
    "WEIGHT_RETRO",
    "is_binding_text",
]
