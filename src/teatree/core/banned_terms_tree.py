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

from teatree.hooks import banned_term_registry
from teatree.hooks.banned_term_registry import MigrationVerification
from teatree.hooks.banned_terms_tree_scan import BannedTermsUnsetError, TreeFinding, load_brand_terms, scan_tree

__all__ = [
    "BannedTermsUnsetError",
    "MigrateRegistryResult",
    "MigrationVerification",
    "TreeFinding",
    "TreeScanResult",
    "migrate_registry",
    "scan_committed_tree",
]


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


def scan_committed_tree(
    repo_root: Path, *, config_path: Path | None = None, allow_unset: bool = False
) -> TreeScanResult:
    """Scan *repo_root*'s committed tree for high-confidence brand names.

    *config_path* overrides the DB path the ``banned_brands`` row is read from
    (else the canonical DB / ``T3_CONFIG_DB``); the ``$TEATREE_BANNED_BRANDS``
    env var may supply the brand list instead. A genuinely-unset brand list (no
    env, no ``banned_brands`` row) propagates :class:`BannedTermsUnsetError` —
    the caller surfaces it LOUD rather than scanning as empty. An explicit
    ``banned_brands = []`` is the deliberate no-brands choice: the brand
    backstop is INERT (``brands_configured=False``) and the always-on
    terminology gate still runs.

    *allow_unset* is the EXPLICIT opt-in that downgrades a genuinely-unset brand
    list from a raise to the INERT terminology-only scan — fail-closed BY DEFAULT
    (``allow_unset=False`` re-raises). The fork-PR CI step passes it because a
    fork cannot read the ``$TEATREE_BANNED_BRANDS`` secret; push/schedule omit it
    so a missing secret on main stays a LOUD refusal. It replaces the dead
    ``T3_BANNED_TERMS_CONFIG`` file fallback (never consumed) with an explicit,
    named flag.
    """
    try:
        terms = load_brand_terms(db_path=config_path)
    except BannedTermsUnsetError:
        if not allow_unset:
            raise
        terms = ()
    return TreeScanResult(findings=scan_tree(repo_root, terms), brands_configured=bool(terms))


@dataclass(frozen=True)
class MigrateRegistryResult:
    """The class-tagged registry a migration produced, plus its self-verification."""

    registry: dict[str, list[str]]
    verification: MigrationVerification


def migrate_registry(*, config_path: Path | None = None) -> MigrateRegistryResult:
    """Build the consolidated banned-term registry from the three legacy sources.

    The core coordinator the CLI (``t3 banned-terms migrate-registry``) calls — the
    CLI layer cannot import ``teatree.hooks`` directly, so the read/build/verify
    logic lives in ``teatree.hooks.banned_term_registry`` and is reached through
    here (``teatree.core`` may import ``teatree.hooks``). Self-verifies the built
    registry reproduces every effective term the old ``banned_terms`` +
    ``banned_brands`` + allowlist yield; the caller FAILS LOUD when the
    verification is not ``ok``. Read-only — it never writes the registry (the
    operator sets it at cutover, PR 2).
    """
    registry = banned_term_registry.build_registry_from_legacy(db_path=config_path)
    verification = banned_term_registry.verify_migration(registry, db_path=config_path)
    return MigrateRegistryResult(registry=registry, verification=verification)
