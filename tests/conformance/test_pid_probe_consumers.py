"""Which modules probe a pid, and do they attribute the namespace first (#4270)?

A pid is namespace-local, so probing one recorded elsewhere answers about whatever
occupies that integer HERE. Twice now that misread reached production: a live worker's
`t3-master` lease read as provably dead from a sibling container (#4253), and the dream
lease releasing a live pass's claim so a second pass ran against the same control DB.
Both were found by looking, and nothing named the set — so fixing them one at a time
leaves the next unattributed probe to ship silently.

This lane names the set from the code rather than from a docstring that can drift: the
consumers are derived by an AST walk for probe references across ``src/teatree``, and a
new one turns it red. Adding a probe is then a deliberate edit here plus an answer to
"whose namespace is that integer in?".

What the walk CANNOT see: a second, unattributed probe inside a module already on the
list, since attribution is asserted per module rather than per call site; a probe outside
``src/teatree``, the only scanned root, so nothing under ``hooks/``; and a raw
``os.kill(pid, 0)``, which is not one of the named helpers. It is a strong
low-false-positive backstop against a NEW probing module, not a proof of total coverage.
"""

import ast

from tests.conformance._src_tree import SRC_DIR, src_modules

#: The liveness probes. A module referencing either asks the kernel about a pid.
_PROBE_API = frozenset({"pid_alive", "pid_alive_probe"})

#: The predicates that answer "does that pid mean anything to me?" — blank-tolerant where
#: a wrong read costs a delayed reclaim, positive-proof where it costs a stolen lease.
_ATTRIBUTION_API = frozenset({"namespace_is_attributable", "namespace_is_proven"})

#: Where the probes and the attribution predicates are DEFINED — definitions, not consumers.
_DEFINITION_MODULES = frozenset({"utils/singleton.py", "core/loop_lease_liveness.py"})

#: The decision layer the attribution predicates must actually live in.
_ATTRIBUTION_HOME = "core/loop_lease_liveness.py"

#: Every module that probes a pid, as repo-relative module paths. Kept EXPLICIT: an
#: unattributed probe is invisible until it releases something it should not have, so a
#: new one is worth one deliberate line here plus a look at whose namespace it reads.
EXPECTED_CONSUMERS = frozenset(
    {
        "core/claim_liveness.py",
        "core/loop_lease_manager.py",
        "eval/regression_corpus_fixtures.py",
        "loop/driver_detection.py",
        "loops/dream/lease.py",
        "loops/live.py",
    }
)

#: Consumers probing a pid THEY chose in THIS process's own namespace, where attribution
#: is the question already answered. ``unused_pid`` walks candidate integers looking for a
#: free one, which is a question only about the reader's own namespace.
SELF_NAMESPACE_PROBES = frozenset({"eval/regression_corpus_fixtures.py"})


def _referenced_names(tree: ast.Module, api: frozenset[str]) -> frozenset[str]:
    """The names in *api* that *tree* references, by bare name or by attribute access.

    Both forms, because a bare-name-only walk is blind to the ``singleton.pid_alive(pid)``
    spelling — the same probe reached through the module rather than imported off it.
    """
    referenced = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Name | ast.Attribute)
    }
    return frozenset(referenced & api)


def _modules_referencing(api: frozenset[str]) -> dict[str, frozenset[str]]:
    """Every ``src/teatree`` module referencing a name in *api*, mapped to the names it uses."""
    found: dict[str, frozenset[str]] = {}
    for path, tree in src_modules():
        relative = path.relative_to(SRC_DIR).as_posix()
        names = _referenced_names(tree, api)
        if names:
            found[relative] = frozenset(names)
    return found


def _probe_consumers() -> frozenset[str]:
    """Every module that probes a pid, excluding the modules that DEFINE the probes."""
    return frozenset(_modules_referencing(_PROBE_API)) - _DEFINITION_MODULES


class TestTheWalkSeesEveryReferenceForm:
    """The derivation is only as wide as the reference shapes it collects."""

    def test_a_bare_name_probe_is_seen(self) -> None:
        tree = ast.parse("from teatree.utils.singleton import pid_alive\npid_alive(7)\n")

        assert _referenced_names(tree, _PROBE_API) == {"pid_alive"}

    def test_an_attribute_style_probe_is_seen(self) -> None:
        tree = ast.parse("from teatree.utils import singleton\nsingleton.pid_alive(7)\n")

        assert _referenced_names(tree, _PROBE_API) == {"pid_alive"}

    def test_a_module_referencing_nothing_in_the_api_is_not_seen(self) -> None:
        # The control: the collector answers NO to something, so a green above is a
        # match rather than a matcher that accepts everything.
        assert _referenced_names(ast.parse("value = 7\n"), _PROBE_API) == frozenset()


class TestPidProbeConsumers:
    def test_the_pid_probing_modules_are_exactly_the_enumerated_set(self) -> None:
        assert _probe_consumers() == EXPECTED_CONSUMERS

    def test_the_walk_can_see_a_consumer_at_all(self) -> None:
        # The control: an empty derivation would satisfy an emptied expectation silently.
        assert _probe_consumers()

    def test_every_consumer_attributes_the_namespace_before_probing(self) -> None:
        attributing = _modules_referencing(_ATTRIBUTION_API)

        for consumer in _probe_consumers() - SELF_NAMESPACE_PROBES:
            assert consumer in attributing, f"{consumer} probes a pid without attributing its namespace"

    def test_the_attribution_predicates_live_in_the_decision_layer(self) -> None:
        # Pins the enumeration to real functions: a renamed predicate reds HERE, naming
        # the rename, instead of reading as "every consumer stopped attributing".
        home = SRC_DIR / _ATTRIBUTION_HOME
        defined = {
            node.name
            for node in ast.walk(ast.parse(home.read_text(encoding="utf-8")))
            if isinstance(node, ast.FunctionDef)
        }

        assert defined >= _ATTRIBUTION_API
