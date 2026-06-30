"""Pre-commit hook: fail when the worktree-lifecycle FSM diagram drifts.

Re-renders the ``Worktree`` FSM block in memory (deterministically) and fails if
any consumer's marked block differs from it — the fix is to run
``generate_fsm_diagrams.py`` and stage the result. The loud local gate; CI
un-masks the same drift via the ``docs-drift`` ``git diff`` step. Mirrors
``check_cli_reference_sync.py``.

See: souliane/teatree#12
"""

import os
import sys
from pathlib import Path

BEGIN = "<!-- BEGIN GENERATED: worktree-fsm -->"
END = "<!-- END GENERATED: worktree-fsm -->"
_CONSUMERS = (
    Path("docs/generated/diagrams/worktree-lifecycle.md"),
    Path("README.md"),
    Path("skills/workspace/SKILL.md"),
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _django_setup() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    src = _repo_root() / "src"
    if src.is_dir():
        sys.path.insert(0, str(src))
    import django

    django.setup()


def find_drift(expected_block: str, consumers: dict[str, str], *, begin: str, end: str) -> list[str]:
    from teatree.core.diagrams import extract_between_markers

    return [
        name
        for name, text in consumers.items()
        if extract_between_markers(text, begin=begin, end=end) != expected_block
    ]


def main() -> int:
    _django_setup()
    from teatree.core.diagrams import fenced_mermaid, render_fsm_mermaid
    from teatree.core.models import Worktree

    root = _repo_root()
    expected = fenced_mermaid(render_fsm_mermaid(Worktree))
    consumers = {str(rel): (root / rel).read_text(encoding="utf-8") for rel in _CONSUMERS}
    drifted = find_drift(expected, consumers, begin=BEGIN, end=END)
    if not drifted:
        return 0

    print("Worktree FSM diagram is out of sync with the Worktree model.")
    print("Regenerate and stage it:")
    print("  uv run python scripts/hooks/generate_fsm_diagrams.py")
    for name in drifted:
        print(f"  drift in: {name}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
