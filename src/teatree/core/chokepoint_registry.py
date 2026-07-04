"""Declarative registry of guarded privileged-action chokepoints (BLUEPRINT §17.4).

Names each privileged runtime action path — the merge keystone first — together
with the callable that performs it, the ordered set of gates that must pass
before it runs, and its verification contract, so the full guard chain is
enumerable and auditable in ONE place. Drift (a gate callable renamed or removed)
is caught by the registry-walk test, which resolves every declared callable
against the live module tree.

This is the SEMANTIC sibling of the static AST manifest
:mod:`teatree.quality.chokepoints` — that one enforces WHICH modules may call a
protected symbol (a call-site concern); this one describes WHAT gates guard the
action and HOW its result is verified (a runtime/audit concern). The two are
orthogonal and intentionally distinct.

Gate and callable references are dotted strings resolved lazily via
:func:`importlib.import_module`, so this module imports nothing from
``teatree.core.merge`` and stays a dependency-graph leaf. Registration is
module-level and idempotent (overwrite by name), so a re-import is a no-op.
"""

import importlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass


class ChokepointResolutionError(ValueError):
    """A declared chokepoint/gate dotted path did not resolve to a callable."""


def _resolve_dotted(path: str) -> Callable[..., object]:
    module_path, _, attr = path.rpartition(".")
    if not module_path or not attr:
        msg = f"chokepoint callable path {path!r} is not a dotted module.attr reference"
        raise ChokepointResolutionError(msg)
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        msg = f"chokepoint callable path {path!r} names a module that does not import: {exc}"
        raise ChokepointResolutionError(msg) from exc
    resolved = getattr(module, attr, None)
    if not callable(resolved):
        msg = f"chokepoint callable path {path!r} does not resolve to a callable"
        raise ChokepointResolutionError(msg)
    return resolved


@dataclass(frozen=True, slots=True)
class GateSpec:
    """One required gate on a guarded chokepoint: a name, the callable, its purpose."""

    name: str
    callable_path: str
    purpose: str

    def resolve(self) -> Callable[..., object]:
        return _resolve_dotted(self.callable_path)


@dataclass(frozen=True, slots=True)
class GuardedChokepoint:
    """A privileged action path plus the gate chain that must pass before it runs."""

    name: str
    callable_path: str
    verification_contract: str
    required_gates: tuple[GateSpec, ...]

    def resolve_callable(self) -> Callable[..., object]:
        return _resolve_dotted(self.callable_path)

    def resolve_gates(self) -> Iterator[Callable[..., object]]:
        for gate in self.required_gates:
            yield gate.resolve()

    def gate_names(self) -> tuple[str, ...]:
        return tuple(gate.name for gate in self.required_gates)


_REGISTRY: dict[str, GuardedChokepoint] = {}


def register_chokepoint(chokepoint: GuardedChokepoint) -> None:
    """Register *chokepoint* under its name; idempotent (overwrite by name)."""
    _REGISTRY[chokepoint.name] = chokepoint


def get_chokepoint(name: str) -> GuardedChokepoint:
    """The registered chokepoint named *name*; :class:`KeyError` when absent."""
    try:
        return _REGISTRY[name]
    except KeyError:
        msg = f"no chokepoint registered under {name!r}; registered: {sorted(_REGISTRY)}"
        raise KeyError(msg) from None


def all_chokepoints() -> tuple[GuardedChokepoint, ...]:
    """Every registered chokepoint, in registration order."""
    return tuple(_REGISTRY.values())


MERGE_KEYSTONE = GuardedChokepoint(
    name="merge_keystone",
    callable_path="teatree.core.merge.execution.merge_ticket_pr",
    verification_contract=(
        "The sole IN_REVIEW→MERGED path (§17.4). Every gate below passes, in order, "
        "against the EXACT live head SHA the review clearance was recorded at, before "
        "the irreversible forge squash-merge. Execution binds to that SHA "
        "(expected_head_oid) and fails closed on head drift; a new push invalidates "
        "clearance until re-cleared. A conflict-only merge-in of origin/main re-binds "
        "the clearance to the merge commit (no re-review); a substantive commit forces "
        "a fresh review. The merge is atomic: CLEAR-consume + MergeAudit + attestation "
        "bind + mark_merged() land together or roll back."
    ),
    required_gates=(
        GateSpec(
            name="public_repo_author_trusted",
            callable_path="teatree.core.merge.authorization.assert_public_repo_author_trusted",
            purpose="On a public repo the PR author must be a trusted identity (§17.4.3 author gate).",
        ),
        GateSpec(
            name="clear_authorized",
            callable_path="teatree.core.merge.authorization._assert_clear_authorized",
            purpose="An actionable, green, independently-issued CLEAR exists; substrate is held (§17.4.3 steps 1+5).",
        ),
        GateSpec(
            name="sha_bind",
            callable_path="teatree.core.merge.sha_bind.verify_sha_bound",
            purpose="The live head equals the reviewed SHA — a new push invalidates clearance (§17.4.3 step 2).",
        ),
        GateSpec(
            name="anti_vacuity",
            callable_path="teatree.core.merge.authorization._assert_anti_vacuity",
            purpose="The SHA-bound anti-vacuity attestation backs the merge (#1829).",
        ),
        GateSpec(
            name="rubric_satisfied",
            callable_path="teatree.core.merge.authorization._assert_rubric_satisfied",
            purpose="The ticket's acceptance-criteria rubric is fully PASS at the head (#2241).",
        ),
        GateSpec(
            name="review_verdict",
            callable_path="teatree.core.merge.authorization.assert_review_verdict_gate",
            purpose="A non-stale independent merge_safe verdict vouches for the live head (#2829).",
        ),
        GateSpec(
            name="no_active_review_lock",
            callable_path="teatree.core.merge.authorization.assert_no_active_review_lock",
            purpose="No review is concurrently in flight for the MR (#1405).",
        ),
    ),
)

register_chokepoint(MERGE_KEYSTONE)
