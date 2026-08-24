"""``dream.engine`` must not re-export ``dream.replay``'s vocabulary.

The #1933 split moved phase 1 into ``replay`` and left a back-compat block
re-exporting seven names so existing callers kept working. `/t3:rules` §
"Deprecated Code" forbids exactly that: delete it completely and update the
callers in the same change. The ``decay`` split in the same branch did it
correctly, which is what makes this one an oversight rather than a convention.

The check is on ``__all__`` — the module's DECLARED surface — not on what happens
to be importable. ``engine`` still imports three replay names because
``run_consolidation`` genuinely calls them; that is a dependency, not a
re-export. Naming them in ``__all__`` would be the re-export.
"""

from teatree.loops.dream import engine, replay


def _defined_in(module: object) -> set[str]:
    """Names whose ``__module__`` is *module* — its own definitions, not its imports."""
    return {
        name
        for name in dir(module)
        if not name.startswith("_") and getattr(getattr(module, name), "__module__", None) == module.__name__
    }


def test_the_probe_can_see_replays_own_definitions() -> None:
    # Anti-vacuity: an empty left-hand set would make the assertion below pass
    # while comparing nothing.
    defined = _defined_in(replay)
    assert {"ConsolidationExtract", "build_extract", "enumerate_members"} <= defined


def test_engine_declares_no_name_that_replay_defines() -> None:
    leaked = sorted(set(engine.__all__) & _defined_in(replay))
    assert not leaked, (
        f"teatree.loops.dream.engine re-exports {leaked}, which teatree.loops.dream.replay defines. "
        "Import them from replay at the call site instead — a back-compat re-export is banned "
        "outright, not deprecated."
    )
