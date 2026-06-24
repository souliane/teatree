"""Pre-commit hook: fail when the CLI changes drift from the generated reference.

Mirrors ``check_antipattern_catalog_sync.py``: the generated
``docs/generated/cli-reference.md`` MUST stay in lockstep with the Typer app. The
hook re-renders the reference in-memory (deterministically) and fails if the
committed file differs — the fix is to run ``generate_cli_reference.py`` and stage
the result. This is the loud local gate; CI un-masks the same drift via the
``docs-drift`` ``git diff`` step (which sets ``CLI_REFERENCE_NO_STAGE``).

See: souliane/teatree#2599
"""

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOC = _REPO_ROOT / "docs" / "generated" / "cli-reference.md"


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
    actual = _DOC.read_text(encoding="utf-8") if _DOC.is_file() else ""

    if expected == actual:
        return 0

    print("CLI reference doc is out of sync with the CLI command tree.")
    print("Regenerate and stage it:")
    print("  uv run python scripts/hooks/generate_cli_reference.py")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
