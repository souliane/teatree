"""The core → backends reaction-publisher inversion registry (#1922).

Fail-SAFE contract: an empty registry returns a no-op publisher so an FSM
transition's reaction side effect never crashes the transition.
"""

from teatree.core import reaction_dispatch


class TestReactionPublisherRegistry:
    def test_backends_ready_registers_the_slack_publisher(self) -> None:
        """``BackendsConfig.ready()`` ran at django.setup() — a real publisher resolves."""
        from teatree.backends.slack.reactions import SlackReactionPublisher  # noqa: PLC0415

        assert isinstance(reaction_dispatch.get_reaction_publisher(), SlackReactionPublisher)

    def test_register_then_get_round_trips(self) -> None:
        class _Fake:
            def add_reactions_for_transition(self, ticket: object, transition_name: str) -> int:
                return 7

            def add_approval_reaction(self, pull_request: object) -> int:
                return 9

        original = reaction_dispatch._publisher
        try:
            reaction_dispatch.register_reaction_publisher(_Fake())
            pub = reaction_dispatch.get_reaction_publisher()
            assert pub.add_reactions_for_transition(object(), "merge") == 7
            assert pub.add_approval_reaction(object()) == 9
        finally:
            reaction_dispatch.register_reaction_publisher(original)

    def test_empty_registry_returns_noop(self) -> None:
        """No publisher registered → both methods are silent no-ops (return 0), never raise."""
        original = reaction_dispatch._publisher
        reaction_dispatch._publisher = None
        try:
            pub = reaction_dispatch.get_reaction_publisher()
            assert pub.add_reactions_for_transition(object(), "merge") == 0
            assert pub.add_approval_reaction(object()) == 0
        finally:
            reaction_dispatch.register_reaction_publisher(original)
