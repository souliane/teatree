"""Repo-SCOPE classifier — forge-host-keyed owned-vs-unknown classification.

SCOPE is one of three orthogonal axes the overlay model keeps separate:

*   **SCOPE** (this module) — owned vs unknown. ``owned`` = the repo's
    ``(host, namespace)`` falls within the overlay's ``owned_repos`` —
    a forge-host-keyed dict of host-relative namespace patterns. An UNKNOWN
    repo is the one the unknown-repo approval gate
    (:mod:`teatree.core.gates.owned_repo_guard`) holds on. Owned does NOT
    mean auto-merge: a shared product repo is in scope yet still needs a
    colleague review, so this classifier gates ONLY the approval decision.
*   **VISIBILITY** — public vs private (``[teatree] private_repos`` +
    ``internal_publish_namespaces``), a leak-prevention concern handled by
    the publish hooks. This axis fails OPEN (unknown → not-private). SCOPE
    fails CLOSED (unknown → ask). Never reuse the visibility verdict here.
*   **COLLABORATION** — solo vs shared, the author/review gate in
    :mod:`teatree.core.review.review_candidate`. Untouched here.

Host-awareness is structural. ``owned.get(host)`` is an EXACT host-key
lookup, so ``gitlab.com/souliane/x`` can never reach a ``github.com``
pattern list — a host-blind entry is impossible to author. The
namespace half reuses
:func:`teatree.hooks._repo_visibility.slug_namespace_matches` verbatim,
applied only AFTER host equality (its host-symmetry is harmless on the
already-host-free namespaces compared here). The single URL chokepoint
:func:`teatree.hooks._repo_visibility.slug_for_cwd` is reused too, so
this module never re-parses a git remote.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from teatree.hooks import _repo_visibility


def normalize_host(raw: str) -> str:
    """Reduce a host (or a host-bearing URL fragment) to its canonical form.

    Lowercases, drops a ``scheme://`` and a ``user@`` prefix, keeps only the
    first ``/``-segment, strips a ``:port``, drops a leading ``www.`` and a
    trailing ``.git`` / ``/``. ``slug_for_cwd`` neither lowercases the host
    nor strips ``www.``, so normalization is applied to BOTH sides of the
    host comparison.
    """
    h = raw.strip().lower()
    if "://" in h:
        h = h.split("://", 1)[1]
    h = h.split("@", 1)[-1]
    h = h.split("/", 1)[0].split(":", 1)[0]
    return h.removeprefix("www.").removesuffix(".git").rstrip("/")


@dataclass(frozen=True, slots=True)
class RepoIdentity:
    """A forge repo's canonical scope key: normalized host + host-stripped namespace."""

    host: str  # normalized canonical host, "" if unresolvable
    namespace: str  # host-stripped "owner/repo", lowercased


def repo_identity_for_cwd(cwd: Path) -> RepoIdentity:
    """Resolve *cwd*'s ``origin`` to a :class:`RepoIdentity` (host + namespace).

    Reuses the single URL chokepoint :func:`slug_for_cwd`. A slug with no
    canonical (dotted) host segment yields an empty identity — an
    unresolvable host is treated as unknown (fail-safe), the same dotted-host
    test :func:`_repo_visibility._strip_host_prefix` uses.
    """
    full = _repo_visibility.slug_for_cwd(cwd)
    if not full:
        return RepoIdentity(host="", namespace="")
    head, sep, rest = full.partition("/")
    if sep and "." in head:
        return RepoIdentity(host=normalize_host(head), namespace=rest.lower())
    return RepoIdentity(host="", namespace="")


def host_aware_owns(owned: dict[str, list[str]], repo: RepoIdentity) -> bool:
    """True iff *repo*'s host is a key in *owned* AND a pattern matches its namespace.

    The host lookup is EXACT-equality (``owned.get(host)``) — THE fix that
    makes a host-blind match impossible. A sole-element ``["*"]`` is the
    whole-host wildcard. Otherwise the namespace half delegates to
    :func:`slug_namespace_matches` (segment-bounded; reused verbatim).
    An empty host or namespace never owns (fail-safe).
    """
    if not repo.host or not repo.namespace:
        return False
    patterns = owned.get(normalize_host(repo.host))
    if patterns is None:
        return False
    if patterns == ["*"]:
        return True
    return any(_repo_visibility.slug_namespace_matches(pat, repo.namespace) for pat in patterns)


def repo_scope(cwd: Path, owned: dict[str, list[str]]) -> Literal["owned", "unknown"]:
    """Classify *cwd* against *owned*: ``owned`` when the forge host+namespace match, else ``unknown``."""
    return "owned" if host_aware_owns(owned, repo_identity_for_cwd(cwd)) else "unknown"


def identity_from_host_and_slug(host: str, slug: str) -> RepoIdentity:
    """Build a :class:`RepoIdentity` from a known host and a bare ``owner/repo`` slug.

    The merge keystone carries the namespace as a bare slug (no host) but the
    host is recoverable from the ticket's issue/PR URL. Both are normalized to
    the canonical scope key. An empty/dotless host (an unresolvable host or a
    bare ssh alias) or an empty slug yields an empty identity (fail-safe →
    unknown), applying the SAME dotted-host requirement as
    :func:`repo_identity_for_cwd` so both paths classify a dotless host
    identically.
    """
    h = normalize_host(host)
    ns = slug.strip().lower().strip("/")
    if not h or "." not in h or not ns:
        return RepoIdentity(host="", namespace="")
    return RepoIdentity(host=h, namespace=ns)
