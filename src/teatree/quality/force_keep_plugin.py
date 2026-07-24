"""The force-keep layer over the tach pytest plugin's deselection (#3672).

The tach plugin (``--tach --tach-base <base>``) deselects, in
``pytest_collection_modifyitems``, every test its reverse-import graph walk cannot
reach from the diff. That graph walk is authoritative — but it cannot know the
escalation contract in :mod:`teatree.quality.affected_tests`: the floor dirs, the
doc-reader mapping, the test-path-mirror rule, and the changed test files themselves
must run regardless of what the graph says.

This plugin applies that contract as a FORCE-KEEP layer, in the SAME session (no second
pytest run, so zero test runs twice). It is a hook WRAPPER around
``pytest_collection_modifyitems``: it hides the force-kept items before the inner hooks
run — so the tach plugin never deselects them and never reports them skipped — then
re-inserts them after. The base is read from the tach plugin's own ``--tach-base``
option, so the two layers diff against the identical ref.

Fail-safe: if the force-keep set cannot be computed (a git error mid-run), the layer
protects the WHOLE collected set — the run degrades to the whole suite rather than let
the plugin deselect on an unproven scope. Loaded only in scoped mode (the CLI decides
FULL-vs-scoped and only then emits ``-p teatree.quality.force_keep_plugin``).
"""

import logging
from collections.abc import Generator
from pathlib import Path

import pytest

from teatree.quality.affected_tests import DEFAULT_BASE, ForceKeep, build_force_keep, classify_selection
from teatree.quality.changed_set import changed_paths

logger = logging.getLogger(__name__)

#: Sentinel for "compute failed" — protect the whole collected set rather than skip-as-pass.
PROTECT_ALL = ForceKeep(paths=(), reasons=(), warnings=("force-keep unavailable — protecting the whole suite",))


def _resolve_base(config: pytest.Config) -> str:
    """The tach plugin's ``--tach-base`` if present, else our shared default ref."""
    try:
        base = config.getoption("--tach-base")
    except ValueError:
        return DEFAULT_BASE
    return base if isinstance(base, str) and base else DEFAULT_BASE


def force_keep_for(root: Path, base: str) -> ForceKeep:
    """The escalation force-keep set for this diff, or :data:`PROTECT_ALL` on any failure."""
    try:
        verdict = classify_selection(changed_paths(base_ref=base, cwd=root))
        return build_force_keep(root, verdict)
    except Exception:
        logger.warning("force-keep set unavailable — protecting the whole collected set (fail-safe)", exc_info=True)
        return PROTECT_ALL


def _rel_posix(item: pytest.Item, root: Path) -> str | None:
    try:
        return item.path.relative_to(root).as_posix()
    except (ValueError, AttributeError):
        return None


def protected_items(items: list[pytest.Item], root: Path, force_keep: ForceKeep) -> list[pytest.Item]:
    """The subset of *items* the force-keep layer keeps over tach's deselection."""
    if force_keep is PROTECT_ALL:
        return list(items)
    kept: list[pytest.Item] = []
    for item in items:
        rel = _rel_posix(item, root)
        if rel is not None and force_keep.covers(rel):
            kept.append(item)
    return kept


@pytest.hookimpl(wrapper=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> Generator[None, object]:
    root = Path(config.rootpath)
    force_keep = force_keep_for(root, _resolve_base(config))
    protected = protected_items(items, root, force_keep)
    protected_ids = {id(item) for item in protected}
    # Hide the force-kept items from the inner hooks (the tach plugin deselects among
    # what it sees), then re-insert them after — so a force-kept test is never removed.
    items[:] = [item for item in items if id(item) not in protected_ids]
    try:
        yield
    finally:
        kept_ids = {id(item) for item in items}
        items.extend(item for item in protected if id(item) not in kept_ids)
