"""Pre-commit hook: fail when the CLI changes drift from the generated reference.

Mirrors ``check_antipattern_catalog_sync.py``: the generated
``docs/generated/cli-reference.md`` MUST stay in lockstep with the Typer app. The
hook re-renders the reference in-memory (deterministically) and compares against
the doc as it sits in the git INDEX — the bytes that will be committed — not the
working tree. Reading the index is what makes the gate un-maskable: a working-tree
regeneration by ``generate_cli_reference.py`` (which, under ``CLI_REFERENCE_NO_STAGE``,
writes but never stages) cannot repair the file out from under the gate and hide a
stale committed reference. The fix on a failure is to run ``generate_cli_reference.py``
and stage the result. CI runs this in the ``docs-drift`` job as a robust gate that
catches committed drift independent of the ``git diff`` step's git-add ordering.

See: souliane/teatree#2599
"""

import os
import sys
from pathlib import Path

from teatree.utils import work_tree

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOC = _REPO_ROOT / "docs" / "generated" / "cli-reference.md"
_DOC_REL = "docs/generated/cli-reference.md"


def _committed_doc(repo_root: Path, rel: str) -> str | None:
    """The doc as staged in the git index, or ``None`` outside a tracked git tree.

    ``git show :<path>`` reads the index entry — the bytes git will record — so a
    working-tree regeneration that did not stage leaves the drifted committed bytes
    here for the gate to catch. ``None`` (no git repo, untracked doc) lets the
    caller fall back to the working tree, e.g. a throwaway test tree.

    The index names *rel* from the work-tree TOP, which is not ``repo_root`` when
    this project is vendored inside a fork — so a bare ``:<rel>`` resolved to
    nothing, the read fell through to the working tree, and the gate compared the
    regenerated doc against itself. :mod:`teatree.utils.work_tree` supplies the
    name git actually recorded.
    """
    try:
        return work_tree.resolve(repo_root).blob("", rel)
    except work_tree.WorkTreeError:
        return None


def main() -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    src = _REPO_ROOT / "src"
    if src.is_dir():
        sys.path.insert(0, str(src))
    import django

    django.setup()

    from teatree.cli import app, register_overlay_commands
    from teatree.cli_reference import render_cli_reference_deterministic

    register_overlay_commands(allowlist={"t3-teatree"})
    expected = render_cli_reference_deterministic(app)

    committed = _committed_doc(_REPO_ROOT, _DOC_REL)
    actual = committed if committed is not None else (_DOC.read_text(encoding="utf-8") if _DOC.is_file() else "")

    if expected == actual:
        return 0

    print("CLI reference doc is out of sync with the CLI command tree.")
    print("Regenerate and stage it:")
    print("  uv run python scripts/hooks/generate_cli_reference.py")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
