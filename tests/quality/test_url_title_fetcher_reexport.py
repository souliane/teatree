"""``scripts/lib/url_title_fetcher.py`` is a real re-export module, not a symlink.

A symlink there pointed one physical file (``src/teatree/url_title_fetcher.py``)
into the ``scripts/`` tree, so ruff walked the SAME inode twice — once under the
``scripts/**`` per-file-ignore scope and once under the ``src/**`` scope. Two
scopes disagreeing on which rules apply is what broke ``ruff check --fix``
idempotency on a pristine checkout. A real re-export module is a distinct
physical file, scanned under exactly one scope.
"""

import importlib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHIM = _REPO_ROOT / "scripts" / "lib" / "url_title_fetcher.py"


def test_shim_is_a_real_file_not_a_symlink() -> None:
    assert _SHIM.is_file()
    assert not _SHIM.is_symlink(), (
        "scripts/lib/url_title_fetcher.py must be a real re-export module, not a symlink into "
        "src/ — a symlinked inode is scanned by ruff under two per-file-ignore scopes and breaks "
        "`ruff check --fix` idempotency."
    )


def test_shim_reexports_the_public_api() -> None:
    shim = importlib.import_module("lib.url_title_fetcher")
    canonical = importlib.import_module("teatree.url_title_fetcher")
    assert shim.enrich_prompt is canonical.enrich_prompt
    assert shim.fetch_titles is canonical.fetch_titles


def test_ruff_walks_no_inode_under_two_scopes() -> None:
    """No two ruff-visible files under ``scripts/`` and ``src/`` share an inode.

    The concrete failure this pins: ``ruff check --show-files`` listed the one
    ``url_title_fetcher`` inode under both paths. A shim re-export severs that.
    """
    scripts_files = {p.resolve() for p in (_REPO_ROOT / "scripts").rglob("*.py")}
    src_files = {p.resolve() for p in (_REPO_ROOT / "src").rglob("*.py")}
    shared = scripts_files & src_files
    assert not shared, f"same physical file reachable under both scripts/ and src/: {sorted(shared)}"
