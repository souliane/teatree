"""Every key written into a model ``extra`` is declared on its ``TypedDict``.

``Ticket.extra`` / ``Worktree.extra`` are JSONFields, so an undeclared key is
accepted at the write and dropped silently by the next transition that rewrites
the field through ``validated_*_extra`` — no error, no log, just a value that is
there until it is not. #4152 shipped a revival cap keyed on such a key: the
counter was written on every revival, stripped by the ``test()`` transition on
the way back round the ladder, and so read ``0`` forever. The cap never fired.

Both write shapes are walked, because covering only one is a guard that reads as
coverage: the direct ``extra["key"] = …`` subscript that shipped the defect, and
the ``merge_extra(set_keys=…)`` locked primitive every other writer uses.

Whole-tree by construction: the write and the declaration sit in different
modules, so no diff-scoped lane sees both ends.
"""

import ast
from pathlib import Path

from teatree.core.models.types import TicketExtra, WorktreeExtra
from tests.conformance._src_tree import REPO_ROOT, src_modules

# Unioned, not per-model: the AST knows the key, never which model the receiver is.
DECLARED_KEYS = frozenset(TicketExtra.__annotations__) | frozenset(WorktreeExtra.__annotations__)

_MODEL_EXTRA_CALLS = {"_extra", "validated_ticket_extra", "validated_worktree_extra"}
_EXTRA_TYPED_DICTS = {"TicketExtra", "WorktreeExtra"}
#: ``merge_extra`` parameters whose keys land in ``extra``; ``pop_keys`` removes, so it cannot strand one.
_MERGE_EXTRA_KEY_PARAMS = {"set_keys", "merge_into_dicts", "append_to_lists"}


def _reads_a_model_extra(node: ast.AST) -> bool:
    """Whether *node* sources a model's ``extra`` field rather than a fresh local dict."""
    return any(
        (isinstance(sub, ast.Attribute) and sub.attr == "extra")
        or (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr in _MODEL_EXTRA_CALLS)
        or (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in _MODEL_EXTRA_CALLS)
        for sub in ast.walk(node)
    )


def _model_extra_names(scope: ast.AST) -> set[str]:
    """Local names in *scope* bound from a model ``extra`` read."""
    names: set[str] = set()
    for node in ast.walk(scope):
        if not isinstance(node, ast.Assign | ast.AnnAssign) or node.value is None:
            continue
        if not _reads_a_model_extra(node.value):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names.update(target.id for target in targets if isinstance(target, ast.Name))
    return names


def _undeclared_subscript_key(target: ast.expr, tracked: set[str]) -> tuple[int, str] | None:
    """The undeclared key *target* writes into a tracked ``extra`` name, when it is such a write."""
    if not (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) and target.value.id in tracked):
        return None
    key = target.slice
    if not isinstance(key, ast.Constant) or not isinstance(key.value, str) or key.value in DECLARED_KEYS:
        return None
    return target.lineno, key.value


def _subscript_writes(tree: ast.Module) -> list[tuple[int, str]]:
    """``<name>["<key>"] = …`` writes, on a name bound from a model ``extra``, with an undeclared key."""
    tracked = _model_extra_names(tree)
    return [
        found
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if (found := _undeclared_subscript_key(target, tracked)) is not None
    ]


def _argument_keys(value: ast.expr) -> list[tuple[int, str]]:
    """Constant keys a ``merge_extra`` argument contributes, as a dict literal or a ``TicketExtra(...)`` call."""
    if isinstance(value, ast.Dict):
        return [(k.lineno, k.value) for k in value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in _EXTRA_TYPED_DICTS:
        return [(value.lineno, kw.arg) for kw in value.keywords if kw.arg]
    return []


def _merge_extra_writes(tree: ast.Module) -> list[tuple[int, str]]:
    """``merge_extra(set_keys={"<key>": …})`` writes with an undeclared key."""
    return [
        (lineno, key)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "merge_extra"
        for kw in node.keywords
        if kw.arg in _MERGE_EXTRA_KEY_PARAMS
        for lineno, key in _argument_keys(kw.value)
        if key not in DECLARED_KEYS
    ]


def _undeclared_writes(path: Path, tree: ast.Module) -> list[str]:
    """Every undeclared model-``extra`` key write in *tree*, as reportable lines."""
    rel = path.relative_to(REPO_ROOT)
    return [
        f"{rel}:{lineno} writes undeclared key '{key}'"
        for lineno, key in sorted(_subscript_writes(tree) + _merge_extra_writes(tree))
    ]


def test_every_model_extra_key_written_in_src_is_declared() -> None:
    findings = [finding for path, tree in src_modules() for finding in _undeclared_writes(path, tree)]

    assert not findings, (
        "Undeclared model ``extra`` key(s) — validated_*_extra will drop these on the next transition:\n"
        + "\n".join(findings)
    )


def test_the_walk_detects_both_write_shapes() -> None:
    """The control: a walk blind to either shape passes the tree above for the wrong reason."""
    planted = ast.parse(
        "def f(ticket):\n"
        "    extra = ticket.extra or {}\n"
        "    extra['zz_undeclared_subscript'] = 1\n"
        "    ticket.merge_extra(set_keys={'zz_undeclared_merge': 2})\n"
        "    ticket.merge_extra(set_keys={'branch': 'declared-so-not-a-finding'})\n"
    )

    assert _undeclared_writes(REPO_ROOT / "planted.py", planted) == [
        "planted.py:3 writes undeclared key 'zz_undeclared_subscript'",
        "planted.py:4 writes undeclared key 'zz_undeclared_merge'",
    ]
