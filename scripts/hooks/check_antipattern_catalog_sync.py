"""Pre-commit hook: fail when antipatterns.yaml drifts from its generated doc.

Mirrors ``check_blueprint_sync.py`` in spirit: the generated
``docs/generated/antipattern-catalog.md`` MUST stay in lockstep with its YAML
source. The hook re-renders the doc in-memory and fails if the committed file
differs — the fix is to run ``generate_antipattern_catalog.py`` and stage the
result.

See: souliane/teatree#166
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOC = _REPO_ROOT / "docs" / "generated" / "antipattern-catalog.md"


def main() -> int:
    sys.path.insert(0, str(_REPO_ROOT / "scripts" / "hooks"))
    from generate_antipattern_catalog import build_markdown

    expected = build_markdown()
    actual = _DOC.read_text(encoding="utf-8") if _DOC.is_file() else ""

    if expected == actual:
        return 0

    print("Anti-pattern catalog doc is out of sync with antipatterns.yaml.")
    print("Regenerate and stage it:")
    print("  uv run python scripts/hooks/generate_antipattern_catalog.py")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
