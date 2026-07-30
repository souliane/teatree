"""Pre-commit hook: generate every FSM lifecycle diagram from its model.

For each :class:`~scripts.hooks.fsm_diagram_specs.DiagramSpec`, renders the
model's django-fsm graph (``teatree.core.diagrams``) and writes the canonical
``docs/generated/diagrams/<slug>-lifecycle.md`` plus the identical fenced block
spliced between that spec's markers in every consumer (``README.md``,
``skills/workspace/SKILL.md``). Auto-stages changed files unless
``FSM_DIAGRAMS_NO_STAGE`` is set — CI sets it for the docs-drift step so
``git diff`` catches a stale committed diagram. Modelled byte-for-byte on
``generate_cli_reference.py``.

See: souliane/teatree#12
"""

import os
import subprocess
import sys
from pathlib import Path

import fsm_diagram_specs


def _write_if_changed(path: Path, content: str, *, stage: bool) -> None:
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if stage:
        subprocess.run(["git", "add", str(path)], check=False)
        print(f"Updated {path}")


def main() -> int:
    fsm_diagram_specs.django_setup()
    from teatree.core.diagrams import MarkerNotFoundError, inject_between_markers

    root = fsm_diagram_specs.repo_root()
    stage = not os.environ.get("FSM_DIAGRAMS_NO_STAGE")

    for spec in fsm_diagram_specs.specs():
        block = spec.block()
        _write_if_changed(root / spec.canonical, spec.canonical_document(block), stage=stage)
        for rel in spec.consumers:
            path = root / rel
            try:
                new_text = inject_between_markers(
                    path.read_text(encoding="utf-8"), begin=spec.begin, end=spec.end, block=block
                )
            except MarkerNotFoundError as exc:
                print(f"ERROR: {rel} {exc}", file=sys.stderr)
                return 1
            _write_if_changed(path, new_text, stage=stage)

    return 0


if __name__ == "__main__":
    sys.exit(main())
