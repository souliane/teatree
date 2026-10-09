"""Pre-commit hook: fail when any FSM lifecycle diagram drifts from its model.

Re-renders each :class:`~scripts.hooks.fsm_diagram_specs.DiagramSpec`'s block in
memory (deterministically) and fails if any consumer's marked block differs from
it — the fix is to run ``generate_fsm_diagrams.py`` and stage the result. The
loud local gate; CI un-masks the same drift via the ``docs-drift`` ``git diff``
step. Mirrors ``check_cli_reference_sync.py``.

See: souliane/teatree#12
"""

import sys

import fsm_diagram_specs


def find_drift(expected_block: str, consumers: dict[str, str], *, begin: str, end: str) -> list[str]:
    from teatree.core.diagrams import extract_between_markers

    return [
        name
        for name, text in consumers.items()
        if extract_between_markers(text, begin=begin, end=end) != expected_block
    ]


def main() -> int:
    fsm_diagram_specs.django_setup()
    root = fsm_diagram_specs.repo_root()

    drifted: list[str] = []
    for spec in fsm_diagram_specs.specs():
        consumers = {str(spec.canonical): (root / spec.canonical).read_text(encoding="utf-8")}
        consumers.update({str(rel): (root / rel).read_text(encoding="utf-8") for rel in spec.consumers})
        drifted.extend(find_drift(spec.block(), consumers, begin=spec.begin, end=spec.end))

    if not drifted:
        return 0

    print("An FSM lifecycle diagram is out of sync with its model.")
    print("Regenerate and stage it:")
    print("  uv run python scripts/hooks/generate_fsm_diagrams.py")
    for name in drifted:
        print(f"  drift in: {name}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
