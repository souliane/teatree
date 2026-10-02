"""Which loop owns a setting — DERIVED from the source, never listed in a table.

B17: *"Loop settings are reachable ONLY through their loop, so the settings-to-loop map
is STRUCTURE, not documentation."* A name-prefix match is not that structure — it files
``review_skill`` (a review-GATE setting) under the ``review`` loop, which is exactly the
mis-grouping the regrouping exists to remove. The structure is WHERE THE KEY IS READ.

The derivation is a fixpoint over the loop layer's own name graph:

*   every definition in ``loops/<name>/`` belongs to ``<name>``;
*   ``domain_jobs``'s ``Domain -> jobs-function`` dict hands each per-overlay loop its
    entry point, and the ``Domain`` member's VALUE is the loop's name, so the edge is
    read off the two sources rather than matched by convention;
*   a definition referenced from an owned definition is owned by the same loop, and one
    two loops reach is owned by NEITHER — the shared chokepoints fall out on their own.

Attribution stops at the loop layer (``teatree.loop`` + ``teatree.loops``). Beyond it the
fixpoint walks into shared gates a single loop happens to be the only caller of, and
claims them; ``send_proxy_mode`` reached the ``dream`` loop that way. Code outside the
layer exists for its own concern, not for the loop that calls it.

A key is loop-owned when EVERY read of it resolves to one loop. ``config/`` is excluded
because a declaration is not a read, and ``core/migrations/`` because a migration is a
frozen record of a past schema rather than a live consumer.

Source-only and import-free by design: it answers for a tree it never imports, so it
costs nothing at runtime and cannot be fooled by a comment mentioning a key.
"""

import ast
import collections
import dataclasses
from collections.abc import Collection, Iterator
from pathlib import Path

#: The packages whose code exists FOR loops — the boundary attribution may not cross.
_LOOP_LAYER = ("loop", "loops")

#: Directories whose mention of a key is not a read: declarations and frozen history.
_NOT_A_READER = (("config",), ("core", "migrations"))

_MAX_FIXPOINT_ROUNDS = 32

type _Site = tuple[Path, str]

#: The scope name a read outside any def is recorded under.
_MODULE_SCOPE = "<module>"


def _python_sources(root: Path) -> Iterator[Path]:
    return (path for path in sorted(root.rglob("*.py")) if "__pycache__" not in path.parts)


def _under(path: Path, root: Path, parts: tuple[str, ...]) -> bool:
    return path.is_relative_to(root.joinpath(*parts))


@dataclasses.dataclass(frozen=True, slots=True)
class _Definition:
    """One top-level function or class, and the names its body reaches."""

    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef

    @property
    def referenced_names(self) -> frozenset[str]:
        reached: set[str] = set()
        for child in ast.walk(self.node):
            if isinstance(child, ast.Name):
                reached.add(child.id)
            elif isinstance(child, ast.Attribute):
                reached.add(child.attr)
        return frozenset(reached)


class LoopLayerIndex:
    """The loop layer's definitions, its name table, and who owns what.

    The name table resolves a local name to the definition it denotes, so a reference
    is followed the way Python follows it — through the module's own imports, including
    the tick-time deferred ones the loops use — rather than by matching a spelling.
    """

    def __init__(self, source_root: Path) -> None:
        self._root = source_root
        self._trees = {path: ast.parse(path.read_text(encoding="utf-8")) for path in _python_sources(source_root)}
        self._layer = frozenset(
            path for path in self._trees if any(_under(path, source_root, (p,)) for p in _LOOP_LAYER)
        )
        self._definitions = self._index_definitions()
        self._names = self._index_names()

    @property
    def loop_names(self) -> tuple[str, ...]:
        """Every mini-loop, identified by the package directory that declares it."""
        loops_dir = self._root / "loops"
        return tuple(sorted(entry.name for entry in loops_dir.iterdir() if (entry / "loop.py").is_file()))

    def _index_definitions(self) -> dict[_Site, _Definition]:
        return {
            (path, node.name): _Definition(node)
            for path in self._layer
            for node in self._trees[path].body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        }

    def _module_path(self, dotted: str) -> Path | None:
        base = self._root.joinpath(*dotted.split(".")[1:])
        return next((c for c in (base.with_suffix(".py"), base / "__init__.py") if c in self._trees), None)

    def _index_names(self) -> dict[Path, dict[str, _Site]]:
        tables: dict[Path, dict[str, _Site]] = collections.defaultdict(dict)
        for path in self._layer:
            table = tables[path]
            for node in ast.walk(self._trees[path]):
                if not (isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("teatree.")):
                    continue
                target = self._module_path(node.module)
                if target in self._layer:
                    table.update({alias.asname or alias.name: (target, alias.name) for alias in node.names})
            for site in self._definitions:
                if site[0] == path:
                    table.setdefault(site[1], site)
        return {
            path: {name: self._through_reexports(target, tables) for name, target in table.items()}
            for path, table in tables.items()
        }

    def _through_reexports(self, target: _Site, tables: dict[Path, dict[str, _Site]]) -> _Site:
        """*target* followed to the module that DEFINES it, past any package re-export.

        An import naming a package that only re-exports the symbol resolves to the
        package, and stopping there loses the whole scanner and every setting it reads.
        """
        seen: set[_Site] = set()
        while target not in self._definitions and target not in seen:
            seen.add(target)
            nxt = tables.get(target[0], {}).get(target[1])
            if nxt is None or nxt == target:
                break
            target = nxt
        return target

    def _domain_loop_names(self) -> dict[str, str]:
        """Each ``Domain`` member paired with its VALUE, which is the loop's own name."""
        identity = self._root / "loop" / "job_identity.py"
        members: dict[str, str] = {}
        for node in self._trees[identity].body:
            if not (isinstance(node, ast.ClassDef) and node.name == "Domain"):
                continue
            for assign in node.body:
                target = assign.targets[0] if isinstance(assign, ast.Assign) and assign.targets else None
                value = assign.value if isinstance(assign, ast.Assign) else None
                if isinstance(target, ast.Name) and isinstance(value, ast.Constant) and isinstance(value.value, str):
                    members[target.id] = value.value
        return members

    def _domain_entry_points(self) -> dict[_Site, str]:
        """Each per-overlay loop's jobs function, off ``domain_jobs``'s own dispatch dict.

        The dict NAMES its builders, and most are defined in a sibling module rather than
        in ``domain_jobs`` — so the name is resolved through that module's import table.
        Seeding the bare ``(domain_jobs, name)`` site instead lands on nothing the fixpoint
        can walk, and the loop reaches none of the settings its entry point reads.
        """
        jobs = self._root / "loop" / "domain_jobs.py"
        members, loops = self._domain_loop_names(), set(self.loop_names)
        table = self._names[jobs]
        entry_points: dict[_Site, str] = {}
        for node in ast.walk(self._trees[jobs]):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=True):
                if not (
                    isinstance(key, ast.Attribute) and isinstance(key.value, ast.Name) and key.value.id == "Domain"
                ):
                    continue
                loop = members.get(key.attr)
                if isinstance(value, ast.Name) and loop in loops and loop is not None:
                    entry_points[table.get(value.id, (jobs, value.id))] = loop
        return entry_points

    def _seed(self) -> dict[_Site, set[str]]:
        owners: dict[_Site, set[str]] = collections.defaultdict(set)
        loops_dir = self._root / "loops"
        for path in self._layer:
            if not path.is_relative_to(loops_dir):
                continue
            package = path.relative_to(loops_dir).parts[0]
            if package in self.loop_names:
                owners[path, _MODULE_SCOPE].add(package)
        for site in self._definitions:
            module_seed = owners.get((site[0], _MODULE_SCOPE))
            if module_seed:
                owners[site] |= module_seed
        for site, loop in self._domain_entry_points().items():
            owners[site].add(loop)
        return owners

    def owner_by_site(self) -> dict[_Site, str]:
        """Every definition a SINGLE loop reaches, and which one — the sole-ownership map."""
        owners = self._seed()
        for _ in range(_MAX_FIXPOINT_ROUNDS):
            grew = False
            for site, loops in list(owners.items()):
                definition = self._definitions.get(site)
                if definition is None:
                    continue
                table = self._names[site[0]]
                for name in definition.referenced_names & table.keys():
                    target = table[name]
                    if target in self._definitions and target != site and not loops <= owners[target]:
                        owners[target] |= loops
                        grew = True
            if not grew:
                break
        return {site: next(iter(loops)) for site, loops in owners.items() if len(loops) == 1}

    def read_sites(self, keys: Collection[str]) -> dict[str, set[_Site]]:
        """Where each of *keys* is read as an attribute or a literal, declarations aside."""
        wanted = frozenset(keys)
        sites: dict[str, set[_Site]] = collections.defaultdict(set)
        for path, tree in self._trees.items():
            if any(_under(path, self._root, parts) for parts in _NOT_A_READER):
                continue
            for key, scope in _reads(tree, wanted):
                sites[key].add((path, scope))
        return sites


def _reads(tree: ast.Module, keys: frozenset[str]) -> Iterator[tuple[str, str]]:
    """Every ``.key`` access and ``"key"`` literal in *tree*, with its top-level scope."""
    scopes: list[str] = []

    def walk(node: ast.AST) -> Iterator[tuple[str, str]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                scopes.append(child.name)
                yield from walk(child)
                scopes.pop()
                continue
            found = (
                child.attr
                if isinstance(child, ast.Attribute) and child.attr in keys
                else child.value
                if isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value in keys
                else None
            )
            if found is not None:
                yield found, scopes[0] if scopes else _MODULE_SCOPE
            yield from walk(child)

    yield from walk(tree)


def loop_owned_settings(source_root: Path, keys: Collection[str]) -> dict[str, str]:
    """*keys* whose every read sits in ONE loop's own code, mapped to that loop's name."""
    index = LoopLayerIndex(source_root)
    owner_by_site = index.owner_by_site()
    owned: dict[str, str] = {}
    for key, sites in index.read_sites(keys).items():
        resolved = [owner_by_site.get(site) for site in sites]
        first = resolved[0] if resolved else None
        if first is not None and all(owner == first for owner in resolved):
            owned[key] = first
    return owned


__all__ = ["LoopLayerIndex", "loop_owned_settings"]
