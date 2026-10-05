"""Coordinator for the full-tree banned-term backstop scan.

The high-confidence leak class comes from ``banned_term_registry`` through
the Django-free hook resolver. Missing configuration always fails loud.
"""

from dataclasses import dataclass
from pathlib import Path

from teatree.hooks import banned_term_registry
from teatree.hooks.banned_terms_tree_scan import (
    BannedTermsUnsetError,
    TreeEnumerationError,
    TreeFinding,
    load_brand_terms,
    scan_tree,
)

__all__ = [
    "BannedTermsUnsetError",
    "TreeEnumerationError",
    "TreeFinding",
    "TreeScanResult",
    "scan_committed_tree",
]


@dataclass(frozen=True)
class TreeScanResult:
    """The outcome of a full-tree backstop scan.

    The scan runs only with a populated leak class. ``findings`` also carries
    the terminology-gate hits.
    """

    findings: list[TreeFinding]


def scan_committed_tree(repo_root: Path, *, config_path: Path | None = None) -> TreeScanResult:
    """Scan *repo_root*'s committed tree for high-confidence brand names.

    *config_path* overrides the DB path for ``banned_term_registry``.
    ``$TEATREE_TERM_REGISTRY`` may supply it instead. An unset registry
    propagates :class:`BannedTermsUnsetError`. An explicit empty leak
    class is also refused before scanning.
    The registry's allow class exempts the company's own identifiers, so this entry
    point honours the escape hatch the other entry points already do.

    A missing registry refuses the scan on every path. An unreadable tree also
    raises :class:`TreeEnumerationError` rather than reporting a clean scan.
    """
    terms = load_brand_terms(db_path=config_path)
    allowlist = banned_term_registry.allowlist_terms(config_path)
    return TreeScanResult(findings=scan_tree(repo_root, terms, allowlist))
