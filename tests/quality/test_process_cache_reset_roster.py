"""Completeness contract for the process-global cache reset roster (TSH-2/TSH-7).

The autouse singleton-reset roster in ``tests/conftest.py`` was reactive
whack-a-mole: every reset landed *after* a specific flake, and nothing forced a
NEW process-global cache to be reset. A cache keyed on ``(ticket.pk, ...)`` is the
worst offender — under sqlite ``TestCase`` rollback rowids recycle, so a stale
entry collides with a later test's fresh pk (the 'green locally, red under a
shard' pollution class).

This gate makes the roster complete-by-construction. It derives the process-global
cache set STRUCTURALLY from ``src/teatree`` — every empty module-level mutable
container (``{}``/``[]``/``set()``/``dict()``…) and every ``@lru_cache``/``@cache``
module function — and asserts each one is either reset by the conftest roster or
explicitly exempt with a stated reason. A new, unclassified cache fails
``test_every_process_cache_is_classified`` until its author decides: reset it
(add it to the roster) or exempt it (say why it is safe to leak).

Structural scope: module-level empty containers and lru-cached module functions,
exactly the shapes a per-test process cache takes. Class-level mutable attributes
and ``None``-initialised singletons (e.g. ``scope_cache._CACHE``, reset separately)
are out of this gate's structural derivation.
"""

# test-path: cross-cutting
import ast
from pathlib import Path

from teatree.core.backend_factory import _code_host_cache, _messaging_cache, reset_backend_caches
from teatree.core.gates.pr_budget_forge import _forge_cache, reset_forge_pr_budget_cache
from teatree.utils.throttled_log import _last_warned, reset_throttle

_SRC = Path(__file__).resolve().parents[2] / "src" / "teatree"
_CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"

#: Constructors that build an empty mutable container when called with no args.
_EMPTY_CONTAINER_CTORS = frozenset(
    {
        "dict",
        "list",
        "set",
        "defaultdict",
        "OrderedDict",
        "WeakValueDictionary",
        "WeakKeyDictionary",
        "Counter",
        "deque",
    },
)
#: Module-function decorators that install a process-lifetime memo.
_MEMO_DECORATORS = frozenset({"lru_cache", "cache"})


def _is_empty_container(node: ast.expr) -> bool:
    """True iff *node* constructs an EMPTY mutable container (a runtime-accumulating cache shape)."""
    if isinstance(node, ast.Dict):
        return not node.keys
    if isinstance(node, (ast.List, ast.Set)):
        return not node.elts
    if isinstance(node, ast.Call) and not node.args and not node.keywords:
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
        return name in _EMPTY_CONTAINER_CTORS
    return False


def _decorator_name(dec: ast.expr) -> str | None:
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def discover_caches_in_source(source: str, module: str) -> dict[str, str]:
    """Map ``"<module>:<symbol>" -> kind`` for every process-cache shape in *source*.

    ``kind`` is ``"container"`` (an empty module-level mutable container, e.g.
    ``_cache: dict = {}``) or ``"lru_cache"`` (a module function decorated with
    ``@lru_cache``/``@cache``). Dunder names (``__all__``) are not state and are
    skipped. Nested (function/class-body) assignments are ignored — only
    module-level, process-global bindings count.
    """
    tree = ast.parse(source, filename=module)
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(_decorator_name(dec) in _MEMO_DECORATORS for dec in node.decorator_list):
                found[f"{module}:{node.name}"] = "lru_cache"
            continue
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not _is_empty_container(value):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and not _is_dunder(target.id):
                found[f"{module}:{target.id}"] = "container"
    return found


def discover_process_caches() -> dict[str, str]:
    """Discover every process-global cache in ``src/teatree`` (see :func:`discover_caches_in_source`)."""
    found: dict[str, str] = {}
    for path in sorted(_SRC.rglob("*.py")):
        module = ".".join(path.relative_to(_SRC.parent).with_suffix("").parts)
        found.update(discover_caches_in_source(path.read_text(encoding="utf-8"), module))
    return found


#: Caches the conftest roster resets around every test, mapped to the reset
#: function conftest invokes (the name that must literally appear in conftest.py).
RESET_BY_CONFTEST: dict[str, str] = {
    "teatree.core.backend_factory:_code_host_cache": "reset_backend_caches",
    "teatree.core.backend_factory:_messaging_cache": "reset_backend_caches",
    "teatree.backends.loader:get_ci_service": "reset_backend_caches",  # cleared transitively by reset_backend_caches
    "teatree.core.overlay_loader:_discover_overlays": "reset_overlay_cache",
    "teatree.core.gates.pr_budget_forge:_forge_cache": "reset_forge_pr_budget_cache",
    "teatree.utils.throttled_log:_last_warned": "reset_throttle",
}

#: Caches deliberately NOT reset, each with the reason it is safe to leave alone.
#: Registries are populated once at import/app-ready and are stable for the whole
#: process — resetting them mid-session would break resolution, not isolate a test.
EXEMPT: dict[str, str] = {
    "teatree.agents.harness_registry:_REGISTRY": "import-populated harness registry; process-stable",
    "teatree.core.factory.chokepoint_registry:_REGISTRY": "import-populated chokepoint registry; process-stable",
    "teatree.core.modelkit.gate_registry:_REGISTRY": "import-populated modelkit gate registry; process-stable",
    "teatree.core.intake.attachment_fetch_registry:_fetchers": "app-ready-populated fetcher registry; process-stable",
    "teatree.core.presence:_FACTORIES": "import-populated presence-factory registry; process-stable",
    "teatree.cli.overlay:OVERLAY_PROXY_COMMANDS": "import-populated command map; not mutated at runtime",
    "teatree.config.settings:TOML_OVERLAY_OVERRIDABLE_SETTINGS": "import-populated constant; not mutated at runtime",
    "teatree.loops.timer_chains:_LIVE_TICK_PGIDS": "live tick subprocess PGIDs; process-lifecycle, not a per-test memo",
}


class TestProcessCacheResetRoster:
    def test_every_process_cache_is_classified(self) -> None:
        discovered = set(discover_process_caches())
        classified = set(RESET_BY_CONFTEST) | set(EXEMPT)
        unclassified = discovered - classified
        stale = classified - discovered
        assert not unclassified, (
            "new process-global cache(s) with no reset roster entry and no exemption — "
            "add each to RESET_BY_CONFTEST (and wire a conftest autouse reset) or to EXEMPT "
            f"with a reason: {sorted(unclassified)}"
        )
        assert not stale, (
            f"classification names a cache that no longer exists in src/teatree — remove it: {sorted(stale)}"
        )

    def test_reset_and_exempt_sets_are_disjoint(self) -> None:
        overlap = set(RESET_BY_CONFTEST) & set(EXEMPT)
        assert not overlap, f"a cache is both reset and exempt: {sorted(overlap)}"

    def test_detector_has_teeth(self) -> None:
        # A synthetic module with one unreset cache and one dunder export: the
        # cache is discovered, the dunder is not. Proves discovery is non-vacuous —
        # add a real `_new_cache = {}` to any src module and the completeness test
        # above goes RED.
        source = "__all__ = []\n_synthetic_cache: dict = {}\nCONST = {'k': 1}\n"
        discovered = discover_caches_in_source(source, "teatree.synthetic")
        assert discovered == {"teatree.synthetic:_synthetic_cache": "container"}

    def test_detector_flags_an_lru_cached_module_function(self) -> None:
        source = "from functools import lru_cache\n\n@lru_cache\ndef memoised():\n    return 1\n"
        discovered = discover_caches_in_source(source, "teatree.synthetic")
        assert discovered == {"teatree.synthetic:memoised": "lru_cache"}

    def test_reset_functions_are_wired_into_conftest(self) -> None:
        conftest_source = _CONFTEST.read_text(encoding="utf-8")
        for cache_id, reset_fn in RESET_BY_CONFTEST.items():
            assert reset_fn in conftest_source, (
                f"{cache_id} is classified reset-by-conftest via {reset_fn}(), but that reset function "
                "is not referenced in tests/conftest.py — wire an autouse fixture that calls it"
            )

    def test_reset_dispositions_actually_clear_their_container_cache(self) -> None:
        # Efficacy: for each plain-dict RESET cache, populate it and prove the
        # roster's reset callable empties it. (The two lru caches are covered by
        # the conftest-wiring check above plus their own module tests.)
        cases = [
            (_forge_cache, ("sentinel", "repo"), reset_forge_pr_budget_cache),
            (_last_warned, "sentinel-key", reset_throttle),
            (_code_host_cache, "sentinel-overlay", reset_backend_caches),
            (_messaging_cache, "sentinel-overlay", reset_backend_caches),
        ]
        for container, key, reset_fn in cases:
            container[key] = object()  # type: ignore[index]
            reset_fn()
            assert key not in container, f"{reset_fn.__name__}() did not clear the cache keyed {key!r}"
