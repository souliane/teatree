"""Which cycles no-op when ``t3-master`` is unheld — the enumerated set (#4253).

An owner-gated cycle whose lease is unheld skips every beat and says so only in a log
line. ``slack-answer`` surfaced at all because a person happens to run it by hand;
``self-improve`` was found twenty minutes later only because the same person looked
again. Nothing named the set, so "what else is silently gated on this?" was unanswerable
at the moment it mattered.

This lane answers it from the code rather than from a docstring that can drift: the
consumers are derived by an AST walk for ``t3_master_verdict`` references across
``src/teatree``, and a third gated cycle turns it red. Adding one is then a deliberate
edit here — and the doctor FAIL that reports the unheld lease names the same set.
"""

import ast

from tests.conformance._src_tree import SRC_DIR, src_modules

#: The gate's decision function. A module referencing it gates its cycle on the lease.
_GATE_API = "t3_master_verdict"

#: The gate module itself and its test-facing re-export home — definitions, not consumers.
_GATE_MODULES = frozenset({"core/gates/t3_master_gate.py"})

#: Every cycle that no-ops on an unheld ``t3-master`` lease, as repo-relative module paths.
#: Kept EXPLICIT: the cost of a silently-gated cycle is a whole loop going dark unnoticed,
#: so a new one is worth one deliberate line here plus a look at whether it should be gated.
EXPECTED_CONSUMERS = frozenset(
    {
        "core/management/commands/loop_slack_answer.py",
        "core/management/commands/loop_self_improve.py",
    }
)


def _loop_command(consumer: str) -> str:
    """The ``t3 loop <name> run`` invocation a ``loop_<name>`` management command answers to."""
    stem = consumer.rsplit("/", 1)[-1].removesuffix(".py").removeprefix("loop_")
    return f"t3 loop {stem.replace('_', '-')} run"


def _gate_consumers() -> frozenset[str]:
    """Every ``src/teatree`` module that reads the gate, as repo-relative paths."""
    consumers: set[str] = set()
    for path, tree in src_modules():
        relative = path.relative_to(SRC_DIR).as_posix()
        if relative in _GATE_MODULES:
            continue
        if any(isinstance(node, ast.Name) and node.id == _GATE_API for node in ast.walk(tree)):
            consumers.add(relative)
    return frozenset(consumers)


class TestT3MasterGateConsumers:
    def test_the_gated_cycles_are_exactly_the_enumerated_set(self) -> None:
        assert _gate_consumers() == EXPECTED_CONSUMERS

    def test_the_walk_can_see_a_consumer_at_all(self) -> None:
        # The control: an empty derivation would satisfy an emptied expectation silently.
        assert _gate_consumers()

    def test_the_doctor_fail_names_every_gated_cycle(self) -> None:
        # The unheld-lease FAIL is the one surface an operator reads, so a cycle that is
        # gated but unnamed there is exactly the silence #4253 was about.
        reported = (SRC_DIR / "cli" / "doctor" / "checks_loop.py").read_text(encoding="utf-8")

        for consumer in EXPECTED_CONSUMERS:
            assert _loop_command(consumer) in reported
