"""Core coordinator for the full-tree banned-brand backstop scan (#1570).

The CLI layer cannot import ``teatree.hooks`` directly (module-boundary
rule); ``teatree.core`` may. This module is the thin coordinator the
``t3 banned-terms scan-tree`` command calls: it resolves the brand list
(env var or ``~/.teatree.toml``) and the config the gate uses, then
delegates the file enumeration + matching to
``teatree.hooks.banned_terms_tree_scan``. It mirrors how
``core.review_findings`` reaches ``hooks.banned_terms_scanner``.
"""

from pathlib import Path

from teatree.hooks import banned_terms_scanner
from teatree.hooks.banned_terms_tree_scan import TreeFinding, load_brand_terms, scan_tree

__all__ = ["TreeFinding", "scan_committed_tree"]


def scan_committed_tree(repo_root: Path, *, config_path: Path | None = None) -> list[TreeFinding]:
    """Scan *repo_root*'s committed tree for high-confidence brand names.

    *config_path* overrides the resolved ``~/.teatree.toml``; when omitted
    the gate's own resolution is used. A missing config is fine — the
    ``$TEATREE_BANNED_BRANDS`` env var may still supply the brand list. With
    no brands from either source the scan is a clean no-op (empty list).
    """
    resolved = config_path if config_path is not None else banned_terms_scanner.resolve_config()
    terms = load_brand_terms(resolved or Path("/nonexistent/.teatree.toml"))
    return scan_tree(repo_root, terms)
