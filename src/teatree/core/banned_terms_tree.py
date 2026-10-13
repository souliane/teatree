"""Coordinator for the full-tree banned-term backstop scan.

The high-confidence leak class comes from ``banned_term_registry`` through
the Django-free hook resolver. Missing configuration always fails loud.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from teatree.hooks import banned_term_registry
from teatree.hooks.banned_terms_tree_scan import (
    BannedTermsUnsetError,
    TreeEnumerationError,
    TreeFinding,
    git_tracked_files,
    load_brand_terms,
    scan_text,
    scan_tree,
)
from teatree.hooks.term_refs import TermRefs
from teatree.utils import work_tree
from teatree.utils.git_remote_ops import remote_slug

__all__ = [
    "VALUE_FREE_ROOTS",
    "BannedTermsUnsetError",
    "TreeEnumerationError",
    "TreeFinding",
    "TreeReadError",
    "TreeScanResult",
    "scan_committed_tree",
    "scan_registry_values",
]

#: The roots that may hold no value of a scanned registry class, the owner's own login excepted.
VALUE_FREE_ROOTS: Final = ("src", "scripts", "tests", "docs", ".github")


class TreeReadError(TreeEnumerationError):
    """A tracked file was unreadable, leaving the registry-value scan incomplete."""

    @classmethod
    def for_path(cls, repo_root: Path, rel: str, exc: OSError) -> "TreeReadError":
        return cls(f"could not read tracked file {rel} under {repo_root}: {exc}")


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
    refs = banned_term_registry.load_term_refs(terms, db_path=config_path)
    return TreeScanResult(findings=scan_tree(repo_root, terms, allowlist, refs))


def scan_registry_values(
    repo_root: Path, classes: tuple[str, ...], *, config_path: Path | None = None
) -> TreeScanResult:
    """Every value of *classes* in the checked-out files under :data:`VALUE_FREE_ROOTS` of *repo_root*'s work tree.

    No allow entry exempts anything here, since the allow class is itself scanned; only exact occurrences of
    the repository owner's login, read from ``origin``, are exempt. An unset registry or classes holding no
    value refuse like the brand scan, and so does a work tree with no tracked file under those roots.
    """
    registry = banned_term_registry.required_registry(db_path=config_path)
    terms = tuple(dict.fromkeys(term for term_class in classes for term in registry[term_class]))
    if not terms:
        msg = f"banned_term_registry holds no {'/'.join(classes)} value — refusing to scan as empty"
        raise BannedTermsUnsetError(msg)
    try:
        top = work_tree.resolve(repo_root).root
    except work_tree.WorkTreeError as exc:
        raise TreeEnumerationError.for_root(repo_root, str(exc)) from exc
    tracked = [path for path in git_tracked_files(top) if path.relative_to(top).parts[0] in VALUE_FREE_ROOTS]
    if not tracked:
        raise TreeEnumerationError.for_root(top, f"no tracked file under {', '.join(VALUE_FREE_ROOTS)}")
    owner = remote_slug(str(top)).partition("/")[0]
    refs = TermRefs.of_classes(registry, classes)
    findings: list[TreeFinding] = []
    for path in tracked:
        rel = path.relative_to(top).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            raise TreeReadError.for_path(top, rel, exc) from exc
        hits = scan_text(text, terms, (owner,) if owner else ())
        findings.extend(TreeFinding(rel, lineno, term, line, refs.ref(term)) for lineno, term, line in hits)
    return TreeScanResult(findings=findings)
