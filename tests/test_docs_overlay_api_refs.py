"""Every ``OverlayBase.<name>`` reference in the prose docs must resolve on the live class.

PR-27b regrouped the optional extension hooks onto composed facets and dropped the
``get_`` prefix, so a doc still naming ``OverlayBase.get_test_command`` points at a
name that no longer exists — an overlay author who overrides it writes dead code the
caller never invokes. The reference is resolved against the imported class rather than
by grepping ``overlay.py``, so a facet ATTRIBUTE (``provisioning``, ``runtime``) and a
method on the base are both accepted without a second pattern to keep in sync.
"""

import re
from pathlib import Path

import pytest

from teatree.core.overlay import OverlayBase

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Prose surfaces an overlay author reads. ``docs/generated`` is excluded: it is
#: rebuilt from the code by ``generate_all_docs``, so it cannot drift on its own.
DOC_GLOBS = ("README.md", "BLUEPRINT.md", "AGENTS.md", "CLAUDE.md", "docs/**/*.md", "skills/**/*.md")

REF = re.compile(r"OverlayBase\.([a-z_][a-z0-9_]*)")


def _doc_paths() -> list[Path]:
    paths: set[Path] = set()
    for pattern in DOC_GLOBS:
        for path in REPO_ROOT.glob(pattern):
            if path.is_file() and "generated" not in path.parts:
                paths.add(path)
    return sorted(paths)


def _facet_home(name: str) -> str:
    """Where the hook actually lives now, for the failure message.

    Tried under both spellings because the regrouping dropped the ``get_`` prefix on
    the behaviour facets but kept it on ``OverlayConfig``'s getters.
    """
    for candidate in (name, name.removeprefix("get_")):
        for facet in OverlayBase._FACET_ATTRS:
            if hasattr(getattr(OverlayBase, facet), candidate):
                return f"overlay.{facet}.{candidate}"
    return "no facet carries it either — the hook is gone, delete the reference"


@pytest.fixture(scope="module")
def doc_refs() -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for path in _doc_paths():
        found.extend((path, name) for name in REF.findall(path.read_text(encoding="utf-8")))
    return found


def test_doc_globs_match_real_files() -> None:
    """Guards the guard: a bad glob would make every assertion below vacuous."""
    paths = _doc_paths()
    assert REPO_ROOT / "README.md" in paths
    assert REPO_ROOT / "BLUEPRINT.md" in paths
    assert any(p.parts[-2] == "overlay-api.md".removesuffix(".md") or p.name == "overlay-api.md" for p in paths)


def test_every_documented_overlay_hook_resolves(doc_refs: list[tuple[Path, str]]) -> None:
    assert doc_refs, "no OverlayBase references found — the scan is broken, not the docs"
    stale = sorted(
        {
            f"{path.relative_to(REPO_ROOT)}: OverlayBase.{name} -> {_facet_home(name)}"
            for path, name in doc_refs
            if not hasattr(OverlayBase, name)
        }
    )
    assert not stale, "pre-facet OverlayBase references that no longer resolve:\n  " + "\n  ".join(stale)
