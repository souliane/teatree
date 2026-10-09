"""Attribute a repo slug to the overlay that DECLARES its forge namespace.

The answer to the shape a repo ENUMERATION cannot cover: an overlay's
``get_workspace_repos()`` is a list, while an overlay owns a forge GROUP, so
every repo created in that group after the table was written attributed to
nothing — and on a multi-overlay install ``""`` does not mean "no overlay", it
means the caller falls through to the ambient one, so a guarded action on an
unmistakably-owned repo was judged by a foreign overlay's config.

The declaration read here is ``owned_repos``, whose keys are forge HOSTS, and the
match honours them: a namespace declared on one forge never attributes a slug
hosted on another. A bare ``owner/repo`` carries no host at all, so the caller
names the FORGE its post is addressed to and only declarations on that forge are
considered.

A pure matcher — the caller hands the declarations in, so nothing here reads or
imports the overlay registry.
"""

from collections.abc import Iterable

from teatree.core.intake.repo_scope import host_aware_owns, identity_from_host_and_slug, normalize_host
from teatree.utils.forge import forge_from_host, normalize_forge

type OverlayScopes = Iterable[tuple[str, dict[str, list[str]]]]


def namespace_owner(url_slug: str, scopes: OverlayScopes, *, forge: str) -> str:
    """The one overlay in *scopes* whose declared *forge* namespace contains ``url_slug``, or ``""``.

    *scopes* is ``(overlay name, its owned_repos)`` for every registered overlay.
    Host-exactness rests ENTIRELY on the ``forge_from_host(host) == forge`` filter
    in :func:`_declarations_on`. The match itself cannot supply it: each identity
    is built from the declaration's OWN host
    (:func:`~teatree.core.intake.repo_scope.identity_from_host_and_slug`), so
    :func:`~teatree.core.intake.repo_scope.host_aware_owns` looks a host up against
    itself and can never miss. Drop that filter and a ``github.com`` namespace
    attributes a GitLab post again. An unrecognised *forge* attributes nothing.

    Within a host the match is segment-bounded and case-folded (``acme`` owns
    ``acme/widget`` but neither ``acme-fork/widget`` nor ``other/acme``). Two
    overlays claiming the namespace is a TIE and returns ``""``: the caller keeps
    whatever resolution it had, so a tie widens nothing.
    """
    if not (forge := normalize_forge(forge)):
        return ""
    matches = {
        name
        for name, host, patterns in _declarations_on(scopes, forge)
        if host_aware_owns({host: patterns}, identity_from_host_and_slug(host, url_slug))
    }
    return next(iter(matches)) if len(matches) == 1 else ""


def _declarations_on(scopes: OverlayScopes, forge: str) -> list[tuple[str, str, list[str]]]:
    """``(overlay, normalized host, patterns)`` for every *forge* declaration, wildcards dropped.

    ``["*"]`` is the whole-host wildcard the SCOPE gate reads as "every repo on
    this host"; as an ATTRIBUTION namespace it would claim every slug on the
    forge, which is a guess rather than ownership, so it never reaches the matcher.
    """
    return [
        (name, normalize_host(host), patterns)
        for name, owned in scopes
        for host, raw in (owned or {}).items()
        if (patterns := [entry for entry in raw or [] if isinstance(entry, str) and entry != "*"])
        and forge_from_host(host) == forge
    ]
