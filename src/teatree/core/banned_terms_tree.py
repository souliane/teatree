"""Core coordinator for the full-tree banned-brand backstop scan (#1570).

The CLI layer cannot import ``teatree.hooks`` directly (module-boundary
rule); ``teatree.core`` may. This module is the thin coordinator the
``t3 banned-terms scan-tree`` command calls: it resolves the brand list
(the ``$TEATREE_BANNED_BRANDS`` env var or the DB-home ``banned_brands``
row) then delegates the file enumeration + matching to
``teatree.hooks.banned_terms_tree_scan``. It mirrors how
``core.review_findings`` reaches ``hooks.banned_terms_scanner``.
"""

from dataclasses import dataclass
from pathlib import Path

from teatree.hooks.banned_terms_tree_scan import BannedTermsUnsetError, TreeFinding, load_brand_terms, scan_tree

__all__ = ["BannedTermsUnsetError", "TreeFinding", "TreeScanResult", "scan_committed_tree"]


@dataclass(frozen=True)
class TreeScanResult:
    """The outcome of a full-tree backstop scan.

    ``brands_configured`` records whether any high-confidence brand was
    supplied (DB row or env var). It is ``False`` when the brand
    backstop is INERT — no ``banned_brands`` populated — so the CLI can
    emit a loud inert signal rather than a silent clean result that hides
    the unpopulated key (#1591). ``findings`` still carries the always-on
    terminology-gate hits regardless of brand configuration.
    """

    findings: list[TreeFinding]
    brands_configured: bool


def scan_committed_tree(repo_root: Path, *, config_path: Path | None = None) -> TreeScanResult:
    """Scan *repo_root*'s committed tree for high-confidence brand names.

    *config_path* overrides the DB path the ``banned_brands`` row is read from
    (else the canonical DB / ``T3_CONFIG_DB``); the ``$TEATREE_BANNED_BRANDS``
    env var may supply the brand list instead. A genuinely-unset brand list (no
    env, no ``banned_brands`` row) propagates :class:`BannedTermsUnsetError` —
    the caller surfaces it LOUD rather than scanning as empty. An explicit
    ``banned_brands = []`` is the deliberate no-brands choice: the brand
    backstop is INERT (``brands_configured=False``) and the always-on
    terminology gate still runs.
    """
    terms = load_brand_terms(db_path=config_path)
    return TreeScanResult(findings=scan_tree(repo_root, terms), brands_configured=bool(terms))
