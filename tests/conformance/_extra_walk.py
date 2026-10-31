"""The AST walkers that find which ``extra`` keys the runtime tree reads and writes.

Shared by the declaration check (every key written or read is declared on its TypedDict) and the
writer check (every key read has a writer). ``Ticket.extra`` / ``Worktree.extra`` are JSONFields,
so every shape a key can be written in is walked: a walk blind to one reads as coverage.
"""

import ast
from collections.abc import Mapping

_MODEL_EXTRA_CALLS = {"_extra", "validated_ticket_extra", "validated_worktree_extra"}
_EXTRA_TYPED_DICTS = {"TicketExtra", "WorktreeExtra"}
#: ``merge_extra`` parameters whose keys land in ``extra``; ``pop_keys`` removes, so it cannot strand one.
_MERGE_EXTRA_KEY_PARAMS = {"set_keys", "merge_into_dicts", "append_to_lists"}
#: Model constructors and managers whose ``extra=`` / ``defaults={"extra": …}`` becomes the row's own.
_MODEL_NAMES = {"Ticket", "Worktree"}
_MODEL_FACTORIES = {"create", "get_or_create", "update_or_create"}
#: A receiver naming one of these owns a TypedDict-constrained ``extra``; other models do not.
_MODEL_RECEIVERS = {"ticket", "worktree"}


def string_constants(tree: ast.Module) -> dict[str, str]:
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


def _subscript_key(target: ast.expr, tracked: set[str], constants: Mapping[str, str]) -> tuple[int, str] | None:
    """The key *target* writes into a tracked ``extra`` name, when it is such a write."""
    if not (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) and target.value.id in tracked):
        return None
    key = _key_name(target.slice, constants)
    return None if key is None else (target.lineno, key)


def _subscript_writes(tree: ast.Module, constants: Mapping[str, str]) -> list[tuple[int, str]]:
    """``<name>["<key>"] = …`` writes on a name bound from a model ``extra``."""
    tracked = _model_extra_names(tree)
    return [
        found
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if (found := _subscript_key(target, tracked, constants)) is not None
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
    """``merge_extra(set_keys={"<key>": …})`` writes."""
    return [
        (lineno, key)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "merge_extra"
        for kw in node.keywords
        if kw.arg in _MERGE_EXTRA_KEY_PARAMS
        for lineno, key in _argument_keys(kw.value, constants)
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
    """``Ticket.objects.get_or_create(defaults={"extra": {…}})`` writes."""
    return [
        (lineno, key)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_model_factory(node.func)
        for value in _constructor_extra_dicts(node)
        for lineno, key in _argument_keys(value, constants)
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


def _annotated_as_extra(annotation: ast.expr | None) -> bool:
    """Whether a parameter annotation, quoted or not, names one of the ``extra`` TypedDicts."""
    return annotation is not None and any(
        (isinstance(sub, ast.Name) and sub.id in _EXTRA_TYPED_DICTS)
        or (
            isinstance(sub, ast.Constant)
            and isinstance(sub.value, str)
            and sub.value.split(" ")[0] in _EXTRA_TYPED_DICTS
        )
        for sub in ast.walk(annotation)
    )


def _typed_extra_names(tree: ast.Module) -> set[str]:
    """Local names bound from an ``extra`` a TypedDict constrains, and parameters annotated as one."""
    names: set[str] = {
        arg.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if _annotated_as_extra(arg.annotation)
    }
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
    return None if key is None else (node.lineno, key)


def read_keys(tree: ast.Module) -> list[tuple[int, str]]:
    """``ticket.extra.get("<key>")`` reads on a typed ``extra``, declared or not."""
    constants, tracked = string_constants(tree), _typed_extra_names(tree)
    return [
        found
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and (found := _read_key(node, tracked, constants))
    ]


def written_keys(tree: ast.Module, constants: Mapping[str, str] | None = None) -> list[tuple[int, str]]:
    """Every key written into a model ``extra`` in *tree* in the shapes a declaration check can trust."""
    constants = string_constants(tree) if constants is None else constants
    return (
        _subscript_writes(tree, constants) + _merge_extra_writes(tree, constants) + _constructor_writes(tree, constants)
    )


_EXTRA_KEYWORDS = frozenset({"extra", "defaults", *_MERGE_EXTRA_KEY_PARAMS})


def _dict_displays(value: ast.expr) -> list[ast.Dict]:
    """The dict displays *value* evaluates to: itself, either arm of a conditional, or a ``cast`` of one."""
    value = _unwrap_cast(value)
    if isinstance(value, ast.Dict):
        return [value]
    if isinstance(value, ast.IfExp):
        return _dict_displays(value.body) + _dict_displays(value.orelse)
    if isinstance(value, ast.BoolOp):
        return [display for operand in value.values for display in _dict_displays(operand)]
    return []


def _display_keys(display: ast.Dict, constants: Mapping[str, str]) -> list[tuple[int, str]]:
    return [(key.lineno, name) for key in display.keys if key and (name := _key_name(key, constants))]


def writer_keys(tree: ast.Module, constants: Mapping[str, str]) -> list[tuple[int, str]]:
    """Every key *tree* could write into an ``extra``: a dict bound to a name or handed to an ``extra`` parameter.

    A declaration check must not flag what it cannot attribute to a model, so it walks the narrow
    shapes above. A writer check errs the other way, because a write reaches an ``extra`` through
    annotated locals, helpers and returned dicts no receiver rule can follow: a dict display that is
    assigned (to a name or an attribute), passed as ``extra=`` / ``defaults=`` / ``set_keys=`` and kin,
    or a subscript store. A display that is only returned or passed positionally overlays a read, it
    persists nothing, so it is not a writer.
    """
    found = written_keys(tree, constants)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            found += [key for display in _dict_displays(node.value) for key in _display_keys(display, constants)]
        elif isinstance(node, ast.Call):
            found += [
                key
                for kw in node.keywords
                if kw.arg in _EXTRA_KEYWORDS
                for display in _dict_displays(kw.value)
                for key in _display_keys(display, constants)
            ]
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Store)
            and (name := _key_name(node.slice, constants)) is not None
        ):
            found.append((node.lineno, name))
    return found
