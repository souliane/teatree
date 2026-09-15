"""Static scan for supersession queries that bypass ``DeferredQuestion.supersedable``.

``supersedable()`` carries the three narrowings a bare ``pending()`` does not (#4721):
never supersede a DELIVERED row (a non-empty ``slack_ts``, which a Slack reply still in
flight binds to), scope by audience so the box's own INTERNAL health queue is left alone,
and match NOTHING rather than silently widening to the whole session when ``run_id`` is
absent. ``pending().filter(session_id=…, run_id=…)`` looks equivalent at the call site and
reintroduces all three — which it did, in a NEW function, within hours of the original fix
(#4749): a loop-driven question was erased before delivery.

The detector walks the attribute chain DOWN from each ``.filter(...)`` call, so an
intermediate ``.order_by(...)`` between the ``pending()`` and the ``filter()`` is caught
too. A filter naming neither ``session_id`` nor ``run_id`` is not a supersession scope and
is never reported — ``pending().filter(audience=…)`` and ``pending().filter(dedupe_marker=…)``
are both legitimate reads.

Stated limit, the same one :mod:`teatree.quality.machine_output_seam` documents about
itself: an AST ratchet only catches the LITERAL shape. A queryset built through a helper,
or a filter whose kwargs are computed (``**scope``), stays invisible. It is a
low-false-positive backstop, not a proof the anti-pattern cannot recur.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

#: The kwargs that make a ``pending()`` filter a SUPERSESSION scope rather than a read.
_SCOPE_KWARGS = frozenset({"session_id", "run_id"})
_PENDING = "pending"
_FILTER = "filter"
#: ``supersedable`` is built FROM ``pending().filter(...)``; it is the seam, not a bypass.
_OWNING_MODULE = "teatree/core/models/deferred_question.py"


@dataclass(frozen=True, order=True)
class SupersessionBypass:
    """One ``pending().filter(...)`` scoped like a supersession, outside the seam."""

    module: str
    lineno: int
    kwargs: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.module}:{self.lineno}:{','.join(self.kwargs)}"


def _reaches_pending(node: ast.expr) -> bool:
    """Whether *node*'s attribute chain bottoms out in a ``.pending(...)`` call."""
    while isinstance(node, ast.Call | ast.Attribute):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == _PENDING:
                return True
            node = node.func
        else:
            node = node.value
    return False


def _scope_kwargs(call: ast.Call) -> tuple[str, ...]:
    return tuple(sorted(kw.arg for kw in call.keywords if kw.arg in _SCOPE_KWARGS))


def _bypasses_in(tree: ast.AST, module: str) -> Iterator[SupersessionBypass]:
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == _FILTER):
            continue
        kwargs = _scope_kwargs(node)
        if kwargs and _reaches_pending(node.func.value):
            yield SupersessionBypass(module=module, lineno=node.lineno, kwargs=kwargs)


def scan_bypasses(*roots: Path) -> list[SupersessionBypass]:
    """Every literal supersession-scoped ``pending().filter(...)`` under *roots*."""
    found: list[SupersessionBypass] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            module = path.as_posix()
            if module.endswith(_OWNING_MODULE):
                continue
            found.extend(_bypasses_in(ast.parse(path.read_text(), module), module))
    return sorted(found)


__all__ = ["SupersessionBypass", "scan_bypasses"]
