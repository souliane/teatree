"""The escalate tool's name and its request kinds; Django-free so a session helper can load them early."""

from typing import Final

from teatree.core.modelkit.owner_decision import OwnerDecision

ESCALATE: Final = "escalate"
INTERNAL_KIND: Final = "internal"
KINDS: Final = (INTERNAL_KIND, *OwnerDecision)
