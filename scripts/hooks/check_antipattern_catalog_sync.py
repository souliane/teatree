"""Pre-commit hook: fail when antipatterns.yaml drifts from its generated doc.

Mirrors ``check_cli_reference_sync.py``: the generated
``docs/generated/antipattern-catalog.md`` MUST stay in lockstep with its YAML
source. The hook re-renders the doc in-memory and compares against BOTH inputs
as they sit in the git INDEX — the bytes that will be committed — not the
working tree. Reading the index is what makes the gate un-maskable: staging a
``antipatterns.yaml`` edit while leaving the regenerated doc unstaged (or an
out-of-band working-tree regeneration) cannot repair the file out from under
the gate and hide a stale committed catalog. The fix on a failure is to run
``generate_antipattern_catalog.py`` and stage the result.

See: souliane/teatree#166
"""

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_YAML_REL = "src/teatree/quality/antipatterns.yaml"
_DOC_REL = "docs/generated/antipattern-catalog.md"


def _committed(repo_root: Path, rel: str) -> str | None:
    """The ``rel`` file as staged in the git index, or ``None`` outside a tracked tree.

    ``git show :<path>`` reads the index entry — the bytes git will record — so a
    working-tree edit that was not staged leaves the drifted committed bytes here
    for the gate to catch. ``None`` (no git repo, untracked path) lets the caller
    fall back to the working tree, e.g. a throwaway test tree.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", f":{rel}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def check(repo_root: Path) -> int:
    from generate_antipattern_catalog import build_markdown

    committed_yaml = _committed(repo_root, _YAML_REL)
    expected = build_markdown(committed_yaml) if committed_yaml is not None else build_markdown()

    committed_doc = _committed(repo_root, _DOC_REL)
    if committed_doc is not None:
        actual = committed_doc
    else:
        doc_path = repo_root / _DOC_REL
        actual = doc_path.read_text(encoding="utf-8") if doc_path.is_file() else ""

    if expected == actual:
        return 0

    print("Anti-pattern catalog doc is out of sync with antipatterns.yaml.")
    print("Regenerate and stage it:")
    print("  uv run python scripts/hooks/generate_antipattern_catalog.py")
    return 1


def main() -> int:
    sys.path.insert(0, str(_REPO_ROOT / "scripts" / "hooks"))
    return check(_REPO_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
