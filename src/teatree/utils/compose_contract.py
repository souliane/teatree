"""Contract between compose templates and the env cache producer set.

Every ``${VAR}`` reference in a ``docker-compose*.yml`` file must either:

1. have a default (``${VAR:-fallback}``) — the compose file handles its own
    absence, so we ignore it, **or**
2. be produced by core (``_declared_core_keys()`` in ``worktree_env``) or by
    an overlay (``OverlayBase.provisioning.declared_env_keys()``).

Anything else is a silent-failure bug: the key is missing at runtime, compose
substitutes empty string, and something downstream misbehaves quietly. This
module makes that class of bug a **CI red** (via ``tests/test_env_contract.py``
or ``t3 overlay contract-check``).
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# ``${VAR}`` — bare, no default. We treat ``${VAR:-…}`` and ``${VAR:?…}`` as
# self-handled and skip them.
_BARE_VAR = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
# ``${VAR:-default}`` or ``${VAR:?err}`` — compose handles these itself.
_WITH_DEFAULT = re.compile(r"\$\{[A-Z_][A-Z0-9_]*:[-?][^}]*\}")


@dataclass(frozen=True, slots=True)
class ComposeVarRef:
    """A single ``${VAR}`` reference in a compose file."""

    var: str
    path: Path
    line: int


@dataclass(frozen=True, slots=True)
class ContractViolation:
    """A reference with no declared producer."""

    var: str
    refs: tuple[ComposeVarRef, ...]

    def format(self) -> str:
        locations = ", ".join(f"{r.path}:{r.line}" for r in self.refs)
        return f"${{{self.var}}} referenced at {locations} — no declared producer"


def extract_refs(path: Path) -> list[ComposeVarRef]:
    """Return every bare ``${VAR}`` reference in *path*.

    Lines with ``${VAR:-default}`` are skipped — the default handles absence.
    Not a full YAML parse; compose substitution is line-based anyway.
    """
    return [
        ComposeVarRef(var=match.group(1), path=path, line=lineno)
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        for match in _BARE_VAR.finditer(_WITH_DEFAULT.sub("", raw))
    ]


def check_contract(
    compose_paths: list[Path],
    *,
    produced: set[str],
    allowed: set[str] | None = None,
) -> list[ContractViolation]:
    """Return every ``${VAR}`` reference not in *produced* or *allowed*.

    *allowed* is for keys that legitimately come from the caller's shell or
    CI environment (e.g. ``CI``, ``HOME``) and would never live in the env
    cache. Anything not in *produced* nor *allowed* is a violation.
    """
    by_var: dict[str, list[ComposeVarRef]] = {}
    for compose in compose_paths:
        for ref in extract_refs(compose):
            by_var.setdefault(ref.var, []).append(ref)

    allowed_set = allowed or set()
    return [
        ContractViolation(var=var, refs=tuple(refs))
        for var, refs in sorted(by_var.items())
        if var not in produced and var not in allowed_set
    ]


def unproduced_declared_keys(declared: set[str], produced_renders: Iterable[Iterable[str]]) -> set[str]:
    """Return declared keys that no generator render actually emits.

    ``check_contract`` guards the *compose → declared* direction: a compose
    file may not reference a key nobody declared. This guards the other,
    silent direction — the *declared → produced* one.

    A key declared in ``_declared_core_keys()`` claims "core always (or
    conditionally) emits me". If the generator stops emitting it (a deleted
    line in ``_core_env_pairs`` / ``render_env_cache``) but the declaration
    stays, ``check_contract`` stays green: the compose file still sees a
    declared producer, yet the value is empty at runtime — exactly the
    weeks-long ``rd:`` / ``POSTGRES_HOST`` drift #390 describes.

    *produced_renders* is the set of key sets the generator emits across the
    relevant conditional branches (e.g. with and without shared postgres). A
    declared key is satisfied if *any* render emits it. Anything declared but
    absent from every render is returned — that set must be empty, or a
    producer was lost.
    """
    union: set[str] = set()
    for render in produced_renders:
        union |= set(render)
    return declared - union
