"""Every ``ConfigSetting`` key ``src/`` names is CLASSIFIED — the inverse walk (#3862/#3867).

``tests/teatree_config/test_stored_row_health.py`` guards the carve-out in ONE
direction: an :data:`INTERNAL_STATE_KEYS` entry whose owner module stopped carrying
its key fails, so an exemption cannot rot into the dead config it exists to prevent.
Nothing guarded the other direction, and that is where the #3867 regression lived: a
key a live module reads and writes but nobody DECLARED falls through every bucket and
renders as ``[unknown — not a declared setting; clear with …]`` — a destructive remedy
printed beside a security-relevant row. ``approval_dial``, ``default_mode`` and
``presence_upgrade_mode`` all shipped with that label; following it un-graduates every
approval class and resets the mode ladder.

The walk derives the key set from the code, never a hand-list, so a NEW undeclared key
fails the PR that introduces it. A key counts when a literal reaches the store's
read/write API — :data:`_ACCESSORS` on one of the :data:`_STORE_SURFACES` — through one
of three resolutions: the literal itself, a module-level constant (in that module or
imported from another), or a same-module helper's parameter whose callers pass such a
constant. A key assembled at runtime from a model field, a dataclass attribute or
operator input is dynamic and out of scope; those surfaces render the note for whatever
row they find rather than naming a key of their own.

A call is an accessor call when its receiver RESOLVES, through the importing module's
own ``from`` imports, to one of those surfaces — never when its receiver merely spells a
familiar name. Matching on the spelling is what let ``cold_reader.read_setting(…)``
through unseen for 34 call sites (#4221): its receiver unparses to ``cold_reader``, which
carries no marker a name-matching walk could recognise, so every key the Django-free
reader names — ``agent_session_permission_mode`` among them — stayed outside the scan.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from teatree.config import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.retired_settings import REMOVED_SETTING_KEYS, RENAMED_SETTING_KEYS
from teatree.config.stored_row_health import INTERNAL_STATE_KEYS

_SRC_DIR = Path(__file__).resolve().parents[2] / "src" / "teatree"

#: The store's read/write API. A literal reaching any of these IS a stored-row key.
_ACCESSORS = frozenset(
    {
        # ConfigSettingManager — the Django hot path.
        "get_effective",
        "set_value",
        "seed",
        "clear",
        # cold_reader — the Django-free cold path, untyped and typed readers alike.
        "read_setting",
        "read_setting_confirmed",
        "bool_setting",
        "int_setting",
        "str_setting",
        "list_setting",
        "mapping_setting",
        "overlay_then_global",
        # cold_writer — the cold path's write twin.
        "write_setting",
    }
)

#: The objects those accessors hang off. A call counts only when its receiver RESOLVES
#: here, so an unrelated ``.clear()`` on a dict is never a config read.
_STORE_SURFACES = frozenset(
    {
        "teatree.config.cold_reader",
        "teatree.config.cold_writer",
        "teatree.core.models.config_setting.ConfigSetting",
    }
)

#: ``(store surface, call form) -> a module the walk must still reach through it``. One
#: entry per RECEIVER shape, so a shape whose matching breaks loses its probe instead of
#: the walk quietly narrowing — the failure #4221 shipped, where every ``cold_reader.``
#: site was invisible while a control built only from manager-shaped keys stayed green.
_SHAPE_PROBES: dict[tuple[str, str], str] = {
    ("teatree.config.cold_reader", "attribute"): "teatree.config.agent_spawn",
    ("teatree.config.cold_reader", "bare"): "teatree.core.gates.main_clone_env",
    ("teatree.config.cold_writer", "attribute"): "teatree.cli.teatree_gate",
    ("teatree.core.models.config_setting.ConfigSetting", "attribute"): "teatree.core.models.loop_preset",
}

#: The modules DEFINING those accessors — their own ``key`` parameter is the API's
#: signature, not a key this tree names.
_ACCESSOR_DEFINITIONS = frozenset(
    {"teatree.config.cold_reader", "teatree.config.cold_writer", "teatree.core.models.config_setting"}
)

#: How far a constant may be chased through re-exporting modules before the scan gives
#: up. Deep enough for the deepest real chain (a key defined once and imported twice).
_MAX_IMPORT_HOPS = 4

type _Sites = dict[str, set[str]]
type _Shape = tuple[str, str]


@dataclass(frozen=True, slots=True)
class AccessorCall:
    """One matched store access: the call node and the ``(surface, form)`` it matched by."""

    call: ast.Call
    shape: _Shape


@dataclass(frozen=True, slots=True)
class ConfigKeyScan:
    """The ``ConfigSetting`` keys ``src/teatree`` names, and the ``module:line`` naming each."""

    trees: dict[str, ast.Module]
    constants: dict[str, dict[str, str]]
    imports: dict[str, dict[str, tuple[str, str]]]

    @classmethod
    def of_src(cls) -> "ConfigKeyScan":
        trees = {
            _dotted_path(path): ast.parse(path.read_text(encoding="utf-8")) for path in sorted(_SRC_DIR.rglob("*.py"))
        }
        return cls(
            trees=trees,
            constants={module: _module_constants(tree) for module, tree in trees.items()},
            imports={module: _module_imports(tree) for module, tree in trees.items()},
        )

    def resolve_constant(self, module: str, name: str, hops: int = 0) -> str | None:
        """The string *name* holds in *module*, chased through re-exporting imports."""
        own = self.constants.get(module, {}).get(name)
        if own is not None:
            return own
        source = self.imports.get(module, {}).get(name)
        if source is None or hops >= _MAX_IMPORT_HOPS:
            return None
        return self.resolve_constant(source[0], source[1], hops + 1)

    def resolve_origin(self, module: str, name: str, hops: int = 0) -> str:
        """The dotted object *name* refers to in *module*, chased through re-exports.

        ``from teatree.core.models import ConfigSetting`` lands on the package's
        ``__init__``, which re-exports it from ``config_setting`` — so both spellings
        resolve to the one surface instead of needing an alias roster.
        """
        source = self.imports.get(module, {}).get(name)
        if source is None or hops >= _MAX_IMPORT_HOPS:
            return f"{module}.{name}"
        return self.resolve_origin(source[0], source[1], hops + 1)

    def accessor_calls(self, module: str, tree: ast.Module) -> Iterator[AccessorCall]:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            shape = self._accessor_shape(module, node.func)
            if shape is not None:
                yield AccessorCall(node, shape)

    def _accessor_shape(self, module: str, callee: ast.expr) -> _Shape | None:
        """The ``(store surface, call form)`` *callee* accesses, or ``None`` when it is not one."""
        if isinstance(callee, ast.Attribute) and callee.attr in _ACCESSORS:
            root = callee.value
            while isinstance(root, ast.Attribute):
                root = root.value
            owner = self.resolve_origin(module, root.id) if isinstance(root, ast.Name) else ""
            return (owner, "attribute") if owner in _STORE_SURFACES else None
        if isinstance(callee, ast.Name) and callee.id in _ACCESSORS:
            owner = self.resolve_origin(module, callee.id).rsplit(".", 1)[0]
            return (owner, "bare") if owner in _STORE_SURFACES else None
        return None

    def scanned_modules(self) -> Iterator[tuple[str, ast.Module]]:
        return ((module, tree) for module, tree in self.trees.items() if module not in _ACCESSOR_DEFINITIONS)

    def keys(self) -> _Sites:
        sites: _Sites = {}
        for module, tree in self.scanned_modules():
            for matched in self.accessor_calls(module, tree):
                for key in self._resolve_key(module, tree, matched.call):
                    sites.setdefault(key, set()).add(f"{module}:{matched.call.lineno}")
        return sites

    def shapes(self) -> dict[_Shape, set[str]]:
        """``{(surface, form): modules}`` — which receiver shapes the walk actually matched."""
        found: dict[_Shape, set[str]] = {}
        for module, tree in self.scanned_modules():
            for matched in self.accessor_calls(module, tree):
                found.setdefault(matched.shape, set()).add(module)
        return found

    def _resolve_key(self, module: str, tree: ast.Module, call: ast.Call) -> set[str]:
        argument = _key_argument(call)
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            return {argument.value}
        if not isinstance(argument, ast.Name):
            return set()
        direct = self.resolve_constant(module, argument.id)
        if direct is not None:
            return {direct}
        return self._resolve_through_callers(module, tree, call, argument.id)

    def _resolve_through_callers(self, module: str, tree: ast.Module, call: ast.Call, name: str) -> set[str]:
        """The constants same-module callers bind to the enclosing helper's *name* parameter.

        ``mode_resolution._setting_name(DEFAULT_MODE_SETTING, …)`` is the shape: the
        helper reads whatever key it is handed, so the keys live at its call sites.
        """
        helper = _enclosing_function(tree, call)
        if helper is None:
            return set()
        position = _parameter_position(helper, name)
        if position is None:
            return set()
        resolved: set[str] = set()
        for bound in _arguments_bound_to(tree, helper.name, position=position, keyword=name):
            if isinstance(bound, ast.Constant) and isinstance(bound.value, str):
                resolved.add(bound.value)
            elif isinstance(bound, ast.Name):
                value = self.resolve_constant(module, bound.id)
                if value is not None:
                    resolved.add(value)
        return resolved


def _dotted_path(path: Path) -> str:
    parts = list(path.relative_to(_SRC_DIR.parent).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _module_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for node in tree.body:
        value = node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value.value
    return constants


def _module_imports(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """``{local_name: (source_module, original_name)}`` for every absolute ``from`` import.

    Walks the whole tree, not just its body: teatree defers ORM imports into function
    bodies, and those are exactly the modules that read the store.
    """
    imports: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                imports[alias.asname or alias.name] = (node.module, alias.name)
    return imports


def _key_argument(call: ast.Call) -> ast.expr | None:
    if call.args:
        return call.args[0]
    # The manager spells the first parameter `key`, the cold reader spells it `name`.
    return next((keyword.value for keyword in call.keywords if keyword.arg in {"key", "name"}), None)


def _enclosing_function(tree: ast.Module, target: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
            child is target for child in ast.walk(node)
        ):
            return node
    return None


def _parameter_position(function: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> int | None:
    positional = [argument.arg for argument in function.args.args]
    if name in positional:
        return positional.index(name)
    return -1 if any(argument.arg == name for argument in function.args.kwonlyargs) else None


def _arguments_bound_to(tree: ast.Module, function: str, *, position: int, keyword: str) -> Iterator[ast.expr]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != function:
            continue
        if 0 <= position < len(node.args):
            yield node.args[position]
        yield from (bound.value for bound in node.keywords if bound.arg == keyword)


class TestEveryConfigSettingKeyIsClassified:
    """A key the tree stores must land in a bucket — the exemption cannot be INCOMPLETE."""

    _SCANNED = ConfigKeyScan.of_src()
    SCAN = _SCANNED.keys()
    SHAPES = _SCANNED.shapes()

    @staticmethod
    def _classified() -> set[str]:
        return (
            set(ALL_KNOWN_CONFIG_SETTINGS)
            | set(REMOVED_SETTING_KEYS)
            | set(RENAMED_SETTING_KEYS)
            | {entry.key for entry in INTERNAL_STATE_KEYS}
        )

    def test_no_key_src_stores_falls_through_every_bucket(self) -> None:
        classified = self._classified()
        unclassified = {key: sorted(sites) for key, sites in self.SCAN.items() if key not in classified}
        assert not unclassified, (
            "these keys are read/written as ConfigSetting rows but declared nowhere, so a stored row "
            f"renders as 'not a declared setting' with a destructive clear remedy: {unclassified}"
        )

    def test_the_walk_resolves_all_three_constant_shapes(self) -> None:
        # Anti-vacuous control: an empty or crippled walk satisfies the assertion above
        # for free. One key per resolution shape the scan claims to cover.
        assert "low_power_preset_name" in self.SCAN, "module-constant resolution is broken"
        assert "approval_dial" in self.SCAN, "cross-module import resolution is broken"
        assert "default_mode" in self.SCAN, "helper-parameter resolution is broken"

    def test_the_walk_still_matches_every_receiver_shape(self) -> None:
        unreached = {
            shape: module for shape, module in _SHAPE_PROBES.items() if module not in self.SHAPES.get(shape, ())
        }
        assert not unreached, (
            "these receiver shapes no longer resolve to a store accessor, so every key read "
            f"through them is invisible to the walk above: {unreached}"
        )

    def test_every_receiver_shape_src_uses_carries_a_probe(self) -> None:
        # The probes are the control's teeth: a shape with none can break unnoticed,
        # and a probe for a shape src no longer uses proves nothing.
        assert set(self.SHAPES) == set(_SHAPE_PROBES)

    def test_a_dynamic_key_is_not_mistaken_for_a_declared_one(self) -> None:
        # The operator-facing surfaces pass whatever row they were handed; naming their
        # parameter as a key would demand a declaration for the string "key" itself.
        assert "key" not in self.SCAN
        assert "setting" not in self.SCAN
