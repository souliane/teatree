"""The escalate tool's name and the kinds a request may name; Django-free so the Codex session helper can load it."""

from typing import Final

from teatree.core.modelkit.owner_decision import OwnerDecision

ESCALATE: Final = "escalate"
INTERNAL_KIND: Final = "internal"
KINDS: Final = (INTERNAL_KIND, *OwnerDecision)
