"""Registry for the Slack transition/approval reaction publisher (#1922).

``core.signals`` posts a Slack emoji reaction as the side effect of an FSM
transition, but the reaction publisher lives in ``teatree.backends`` (the higher
layer). Rather than ``core`` importing ``backends``, ``backends`` registers its
publisher here at app-ready time and ``signals`` resolves it when a transition
fires.

Fail-SAFE: a reaction is a non-fatal side effect (the on-behalf gate already
governs whether it posts), so the registry returns a no-op publisher when nothing
is registered — the FSM transition must always commit even with no publisher.
"""

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from teatree.core.models.pull_request import PullRequest
    from teatree.core.models.ticket import Ticket


class ReactionPublisher(Protocol):
    def add_reactions_for_transition(self, ticket: "Ticket", transition_name: str) -> int: ...  # pragma: no branch

    def add_approval_reaction(self, pull_request: "PullRequest") -> int: ...  # pragma: no branch


class _NoopReactionPublisher:
    def add_reactions_for_transition(self, ticket: "Ticket", transition_name: str) -> int:  # noqa: ARG002, PLR6301
        return 0

    def add_approval_reaction(self, pull_request: "PullRequest") -> int:  # noqa: ARG002, PLR6301
        return 0


_NOOP: ReactionPublisher = _NoopReactionPublisher()
_publisher: ReactionPublisher | None = None


def register_reaction_publisher(publisher: ReactionPublisher) -> None:
    global _publisher  # noqa: PLW0603 — single process-wide publisher registered at app-ready
    _publisher = publisher


def get_reaction_publisher() -> ReactionPublisher:
    return _publisher if _publisher is not None else _NOOP
