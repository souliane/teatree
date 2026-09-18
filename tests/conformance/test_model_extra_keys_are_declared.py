"""Every key written into — or read out of — a model ``extra`` is declared on its ``TypedDict``.

``Ticket.extra`` / ``Worktree.extra`` are JSONFields, so an undeclared key is
accepted at the write and dropped silently by the next transition that rewrites
the field through ``validated_*_extra`` — no error, no log, just a value that is
there until it is not. #4152 shipped a revival cap keyed on such a key: the
counter was written on every revival, stripped by the ``test()`` transition on
the way back round the ladder, and so read ``0`` forever. The cap never fired.

Every write shape is walked, because covering only some is a guard that reads as
coverage. #2663 measured that: with only the subscript and ``merge_extra``
literal shapes walked, ``slack_answer`` sat undeclared on 50 live tickets —
written through a constant-named key and a ``get_or_create`` defaults dict,
neither of which the walk could see, and read back at five sites that could only
ever return what the last transition had not yet stripped.

Reads are walked for that last reason: reading a key nothing declares is dead by
construction, so it is a defect wherever the write is. They need a tighter
receiver rule than writes — ``Directive``, ``OutboundClaim`` and
``OuterLoopExperiment`` carry their own ``extra`` JSON that no TypedDict
constrains, so only a receiver provably naming a Ticket/Worktree one counts.

Whole-tree by construction: the write and the declaration sit in different
modules, so no diff-scoped lane sees both ends.
"""

import ast
from collections.abc import Mapping
from pathlib import Path

from teatree.core.models.types import TicketExtra, WorktreeExtra
from tests.conformance._src_tree import REPO_ROOT, src_modules

# Unioned, not per-model: the AST knows the key, never which model the receiver is.
DECLARED_KEYS = frozenset(TicketExtra.__annotations__) | frozenset(WorktreeExtra.__annotations__)

_MODEL_EXTRA_CALLS = {"_extra", "validated_ticket_extra", "validated_worktree_extra"}
_EXTRA_TYPED_DICTS = {"TicketExtra", "WorktreeExtra"}
#: ``merge_extra`` parameters whose keys land in ``extra``; ``pop_keys`` removes, so it cannot strand one.
_MERGE_EXTRA_KEY_PARAMS = {"set_keys", "merge_into_dicts", "append_to_lists"}
#: Model constructors and managers whose ``extra=`` / ``defaults={"extra": …}`` becomes the row's own.
_MODEL_NAMES = {"Ticket", "Worktree"}
_MODEL_FACTORIES = {"create", "get_or_create", "update_or_create"}
#: A receiver naming one of these owns a TypedDict-constrained ``extra``; other models do not.
_MODEL_RECEIVERS = {"ticket", "worktree"}


def _string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, so a key held in a constant still resolves."""
    return {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _key_name(node: ast.expr, constants: Mapping[str, str]) -> str | None:
    """The string key *node* denotes, written literally or held in a module constant."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    return constants.get(node.id) if isinstance(node, ast.Name) else None


def _unwrap_cast(value: ast.expr) -> ast.expr:
    """``cast("TicketExtra", <expr>)`` unwrapped — the cast asserts the keys, it does not check them."""
    if isinstance(value, ast.Call) and len(value.args) == 2:
        func = value.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "cast":
            return value.args[1]
    return value


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


def _undeclared_subscript_key(
    target: ast.expr, tracked: set[str], constants: Mapping[str, str]
) -> tuple[int, str] | None:
    """The undeclared key *target* writes into a tracked ``extra`` name, when it is such a write."""
    if not (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) and target.value.id in tracked):
        return None
    key = _key_name(target.slice, constants)
    return None if key is None or key in DECLARED_KEYS else (target.lineno, key)


def _subscript_writes(tree: ast.Module, constants: Mapping[str, str]) -> list[tuple[int, str]]:
    """``<name>["<key>"] = …`` writes, on a name bound from a model ``extra``, with an undeclared key."""
    tracked = _model_extra_names(tree)
    return [
        found
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if (found := _undeclared_subscript_key(target, tracked, constants)) is not None
    ]


def _argument_keys(value: ast.expr, constants: Mapping[str, str]) -> list[tuple[int, str]]:
    """Keys an ``extra``-bound argument contributes, as a dict literal or a ``TicketExtra(...)`` call."""
    value = _unwrap_cast(value)
    if isinstance(value, ast.Dict):
        return [(key.lineno, name) for key in value.keys if key and (name := _key_name(key, constants))]
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in _EXTRA_TYPED_DICTS:
        return [(value.lineno, kw.arg) for kw in value.keywords if kw.arg]
    return []


def _merge_extra_writes(tree: ast.Module, constants: Mapping[str, str]) -> list[tuple[int, str]]:
    """``merge_extra(set_keys={"<key>": …})`` writes with an undeclared key."""
    return [
        (lineno, key)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "merge_extra"
        for kw in node.keywords
        if kw.arg in _MERGE_EXTRA_KEY_PARAMS
        for lineno, key in _argument_keys(kw.value, constants)
        if key not in DECLARED_KEYS
    ]


def _is_model_factory(func: ast.expr) -> bool:
    """``Ticket(…)`` / ``Ticket.objects.get_or_create(…)`` — a call whose ``extra`` becomes a row's own."""
    if isinstance(func, ast.Name):
        return func.id in _MODEL_NAMES
    if not (isinstance(func, ast.Attribute) and func.attr in _MODEL_FACTORIES):
        return False
    owner = func.value
    return (
        isinstance(owner, ast.Attribute)
        and owner.attr == "objects"
        and isinstance(owner.value, ast.Name)
        and owner.value.id in _MODEL_NAMES
    )


def _constructor_extra_dicts(node: ast.Call) -> list[ast.expr]:
    """The ``extra`` dicts *node* passes, written directly or nested under ``defaults``."""
    found: list[ast.expr] = []
    for kw in node.keywords:
        if kw.arg == "extra":
            found.append(kw.value)
        elif kw.arg == "defaults" and isinstance(kw.value, ast.Dict):
            found.extend(
                value
                for key, value in zip(kw.value.keys, kw.value.values, strict=True)
                if isinstance(key, ast.Constant) and key.value == "extra"
            )
    return found


def _constructor_writes(tree: ast.Module, constants: Mapping[str, str]) -> list[tuple[int, str]]:
    """``Ticket.objects.get_or_create(defaults={"extra": {…}})`` writes with an undeclared key."""
    return [
        (lineno, key)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_model_factory(node.func)
        for value in _constructor_extra_dicts(node)
        for lineno, key in _argument_keys(value, constants)
        if key not in DECLARED_KEYS
    ]


def _is_model_receiver(node: ast.expr) -> bool:
    """Whether ``<node>.extra`` is a Ticket/Worktree ``extra`` rather than another model's own JSON."""
    if isinstance(node, ast.Name):
        return node.id in _MODEL_RECEIVERS
    return isinstance(node, ast.Attribute) and node.attr in _MODEL_RECEIVERS


def _typed_extra_call(node: ast.Call) -> bool:
    """Whether a call yields the extra itself — a validator, or a ``cast``/``dict`` wrapping one."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name in _MODEL_EXTRA_CALLS:
        return True
    if name == "cast" and len(node.args) == 2:
        return _typed_extra_sources(node.args[1])
    return name == "dict" and bool(node.args) and _typed_extra_sources(node.args[0])


def _typed_extra_sources(node: ast.expr) -> bool:
    """Whether *node* evaluates to the ``extra`` mapping itself, on a model a TypedDict constrains.

    Structural rather than a walk, because the distinction that matters is between the
    extra and a value taken OUT of it: ``extra.get("visual_qa")`` yields a nested
    TypedDict whose own keys are declared on that TypedDict, not on ``TicketExtra``.
    """
    if isinstance(node, ast.BoolOp):
        return any(_typed_extra_sources(value) for value in node.values)
    if isinstance(node, ast.IfExp):
        return _typed_extra_sources(node.body) or _typed_extra_sources(node.orelse)
    if isinstance(node, ast.Attribute):
        return node.attr == "extra" and _is_model_receiver(node.value)
    return isinstance(node, ast.Call) and _typed_extra_call(node)


def _typed_extra_names(tree: ast.Module) -> set[str]:
    """Local names bound from an ``extra`` a TypedDict constrains."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign | ast.AnnAssign) or node.value is None:
            continue
        if not _typed_extra_sources(node.value):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names.update(target.id for target in targets if isinstance(target, ast.Name))
    return names


def _read_key(node: ast.Call, tracked: set[str], constants: Mapping[str, str]) -> tuple[int, str] | None:
    """The key a ``<extra>.get("<key>")`` call reads, when its receiver is a typed ``extra``."""
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "get" and node.args):
        return None
    receiver = func.value
    named = isinstance(receiver, ast.Name) and receiver.id in tracked
    if not (named or _typed_extra_sources(receiver)):
        return None
    key = _key_name(node.args[0], constants)
    return None if key is None or key in DECLARED_KEYS else (node.lineno, key)


def _undeclared_reads(tree: ast.Module, constants: Mapping[str, str]) -> list[tuple[int, str]]:
    """``ticket.extra.get("<key>")`` reads of a key no TypedDict declares — dead by construction."""
    tracked = _typed_extra_names(tree)
    return [
        found
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and (found := _read_key(node, tracked, constants))
    ]


def _undeclared_writes(path: Path, tree: ast.Module) -> list[str]:
    """Every undeclared model-``extra`` key write in *tree*, as reportable lines."""
    rel = path.relative_to(REPO_ROOT)
    constants = _string_constants(tree)
    found = _subscript_writes(tree, constants) + _merge_extra_writes(tree, constants)
    return [
        f"{rel}:{lineno} writes undeclared key '{key}'"
        for lineno, key in sorted(found + _constructor_writes(tree, constants))
    ]


def _undeclared_read_lines(path: Path, tree: ast.Module) -> list[str]:
    """Every undeclared model-``extra`` key read in *tree*, as reportable lines."""
    rel = path.relative_to(REPO_ROOT)
    return [
        f"{rel}:{lineno} reads undeclared key '{key}'"
        for lineno, key in sorted(_undeclared_reads(tree, _string_constants(tree)))
    ]


def test_every_model_extra_key_written_in_src_is_declared() -> None:
    findings = [finding for path, tree in src_modules() for finding in _undeclared_writes(path, tree)]

    assert not findings, (
        "Undeclared model ``extra`` key(s) — validated_*_extra will drop these on the next transition:\n"
        + "\n".join(findings)
    )


def test_every_model_extra_key_read_in_src_is_declared() -> None:
    findings = [finding for path, tree in src_modules() for finding in _undeclared_read_lines(path, tree)]

    assert not findings, (
        "Undeclared model ``extra`` key(s) read — no write of these survives a transition, so the read is dead:\n"
        + "\n".join(findings)
    )


def test_the_walk_detects_every_write_shape() -> None:
    """The control: a walk blind to any one shape passes the tree above for the wrong reason."""
    planted = ast.parse(
        "_KEY = 'zz_undeclared_constant'\n"
        "def f(ticket):\n"
        "    extra = ticket.extra or {}\n"
        "    extra['zz_undeclared_subscript'] = 1\n"
        "    extra[_KEY] = 2\n"
        "    ticket.merge_extra(set_keys={'zz_undeclared_merge': 3})\n"
        "    ticket.merge_extra(merge_into_dicts={_KEY: 4})\n"
        "    ticket.merge_extra(set_keys=cast('TicketExtra', {'zz_undeclared_cast': 5}))\n"
        "    Ticket.objects.get_or_create(url=1, defaults={'extra': {'zz_undeclared_defaults': 6}})\n"
        "    Worktree(extra={'zz_undeclared_ctor': 7})\n"
        "    ticket.merge_extra(set_keys={'branch': 'declared-so-not-a-finding'})\n"
        "    Ticket.objects.create(extra={'labels': ['declared-so-not-a-finding']})\n"
        "    ticket.merge_extra(pop_keys=['zz_popped_is_not_a_write'])\n"
    )

    assert _undeclared_writes(REPO_ROOT / "planted.py", planted) == [
        "planted.py:4 writes undeclared key 'zz_undeclared_subscript'",
        "planted.py:5 writes undeclared key 'zz_undeclared_constant'",
        "planted.py:6 writes undeclared key 'zz_undeclared_merge'",
        "planted.py:7 writes undeclared key 'zz_undeclared_constant'",
        "planted.py:8 writes undeclared key 'zz_undeclared_cast'",
        "planted.py:9 writes undeclared key 'zz_undeclared_defaults'",
        "planted.py:10 writes undeclared key 'zz_undeclared_ctor'",
    ]


def test_the_walk_detects_undeclared_reads_without_flagging_other_models() -> None:
    """The control: reads are found on a typed ``extra``, and only on one."""
    planted = ast.parse(
        "_KEY = 'zz_undeclared_constant'\n"
        "def f(ticket, task, audit):\n"
        "    ticket.extra.get('zz_undeclared_direct')\n"
        "    task.ticket.extra.get(_KEY)\n"
        "    extra = ticket.extra if isinstance(ticket.extra, dict) else {}\n"
        "    extra.get('zz_undeclared_via_name')\n"
        "    ticket.extra.get('branch')\n"
        "    audit.extra.get('zz_other_model_is_not_ours')\n"
    )

    assert _undeclared_read_lines(REPO_ROOT / "planted.py", planted) == [
        "planted.py:3 reads undeclared key 'zz_undeclared_direct'",
        "planted.py:4 reads undeclared key 'zz_undeclared_constant'",
        "planted.py:6 reads undeclared key 'zz_undeclared_via_name'",
    ]
