"""Core coordinator for the full-tree banned-brand backstop scan (#1570).

The CLI layer cannot import ``teatree.hooks`` directly (module-boundary
rule); ``teatree.core`` may. This module is the thin coordinator the
``t3 banned-terms scan-tree`` command calls: it resolves the brand list
(env var or ``~/.teatree.toml``) and the config the gate uses, then
delegates the file enumeration + matching to
``teatree.hooks.banned_terms_tree_scan``. It mirrors how
``core.review_findings`` reaches ``hooks.banned_terms_scanner``.
"""

from dataclasses import dataclass
from pathlib import Path

from teatree.hooks import banned_terms_scanner
from teatree.hooks.banned_terms_tree_scan import BannedTermsUnsetError, TreeFinding, load_brand_terms, scan_tree

__all__ = ["BannedTermsUnsetError", "TreeFinding", "TreeScanResult", "scan_committed_tree"]


@dataclass(frozen=True)
class TreeScanResult:
    """The outcome of a full-tree backstop scan.

    ``brands_configured`` records whether any high-confidence brand was
    supplied (config key or env var). It is ``False`` when the brand
    backstop is INERT — no ``banned_brands`` populated — so the CLI can
    emit a loud inert signal rather than a silent clean result that hides
    the unpopulated key (#1591). ``findings`` still carries the always-on
    terminology-gate hits regardless of brand configuration.
    """

    findings: list[TreeFinding]
    brands_configured: bool


def scan_committed_tree(repo_root: Path, *, config_path: Path | None = None) -> TreeScanResult:
    """Scan *repo_root*'s committed tree for high-confidence brand names.

    *config_path* overrides the resolved ``~/.teatree.toml``; when omitted
    the gate's own resolution is used. The ``$TEATREE_BANNED_BRANDS`` env var
    may supply the brand list when no config exists. A genuinely-unset brand
    list (no config, no env, a missing key) propagates
    :class:`BannedTermsUnsetError` — the caller surfaces it LOUD rather
    than scanning as empty. An explicit ``banned_brands = []`` is the
    deliberate no-brands choice: the brand backstop is INERT
    (``brands_configured=False``) and the always-on terminology gate still runs.
    """
    resolved = config_path if config_path is not None else banned_terms_scanner.resolve_config()
    terms = load_brand_terms(resolved or Path("/nonexistent/.teatree.toml"))
    return TreeScanResult(findings=scan_tree(repo_root, terms), brands_configured=bool(terms))
