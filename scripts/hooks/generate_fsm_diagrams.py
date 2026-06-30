"""Pre-commit hook: generate the worktree-lifecycle FSM diagram from the model.

Renders the ``Worktree`` django-fsm graph (``teatree.core.diagrams``) and writes
the canonical ``docs/generated/diagrams/worktree-lifecycle.md`` plus the
identical fenced block spliced between the worktree-fsm markers in every
consumer (``README.md``, ``skills/workspace/SKILL.md``). Auto-stages changed
files unless ``FSM_DIAGRAMS_NO_STAGE`` is set — CI sets it for the docs-drift
step so ``git diff`` catches a stale committed diagram. Modelled byte-for-byte
on ``generate_cli_reference.py``.

See: souliane/teatree#12
"""

import os
import subprocess
import sys
from pathlib import Path

BEGIN = "<!-- BEGIN GENERATED: worktree-fsm -->"
END = "<!-- END GENERATED: worktree-fsm -->"
_CANONICAL = Path("docs/generated/diagrams/worktree-lifecycle.md")
_CANONICAL_TITLE = "Worktree lifecycle"
_CONSUMERS = (Path("README.md"), Path("skills/workspace/SKILL.md"))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _django_setup() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    src = _repo_root() / "src"
    if src.is_dir():
        sys.path.insert(0, str(src))
    import django

    django.setup()


def _worktree_fsm_block() -> str:
    from teatree.core.diagrams import fenced_mermaid, render_fsm_mermaid
    from teatree.core.models import Worktree

    return fenced_mermaid(render_fsm_mermaid(Worktree))


def _write_if_changed(path: Path, content: str, *, stage: bool) -> None:
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if stage:
        subprocess.run(["git", "add", str(path)], check=False)
        print(f"Updated {path}")


def main() -> int:
    _django_setup()
    from teatree.core.diagrams import MarkerNotFoundError, inject_between_markers

    root = _repo_root()
    block = _worktree_fsm_block()
    stage = not os.environ.get("FSM_DIAGRAMS_NO_STAGE")

    _write_if_changed(root / _CANONICAL, f"# {_CANONICAL_TITLE}\n\n{BEGIN}\n{block}\n{END}\n", stage=stage)

    for rel in _CONSUMERS:
        path = root / rel
        try:
            new_text = inject_between_markers(path.read_text(encoding="utf-8"), begin=BEGIN, end=END, block=block)
        except MarkerNotFoundError as exc:
            print(f"ERROR: {rel} {exc}", file=sys.stderr)
            return 1
        _write_if_changed(path, new_text, stage=stage)

    return 0


if __name__ == "__main__":
    sys.exit(main())
