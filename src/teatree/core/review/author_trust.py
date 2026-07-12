"""Public-repo author-trust classifier — the shared seam (#1773).

The merge keystone and the four reviewing scanners (``pr_sweep``,
``codex_review``, ``slack_broadcasts``, the mechanical handlers) all need the
SAME answer to one question: "on this repo, is this PR author the user?" One
helper owns that decision so the consumers cannot drift apart.

Trust model (BLUEPRINT §17.4). On a PUBLIC repo, anyone who is not the user is
a potential malicious actor; the author must be a trusted identity to be
auto-mergeable. On a PRIVATE / internal repo the user controls access, so there
is NO author check — any author is allowed.

Resolution. Visibility uses :func:`slug_is_allowlisted_private` (offline,
recommended) first, then the day-cached ``gh``/``glab`` probe via
:func:`slug_is_private`; an UNRESOLVABLE visibility is treated as PUBLIC (the
safe direction here: require trust). The trust set is DB :class:`TrustedIdentity`
rows first; an EMPTY table or a pre-migration database error falls back to the
configured ``user_identity_aliases`` so the migration window never regresses.

Fail-closed: an EMPTY / unknown author on a public repo is untrusted; the
caller (the keystone, the sweep) refuses the auto-merge.
"""

import logging
from dataclasses import dataclass

from django.apps import apps
from django.db import OperationalError, ProgrammingError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuthorClassification:
    """The verdict for one ``(slug, author)`` pair.

    Exactly the shape the consumers branch on. ``internal_repo`` is True for a
    PRIVATE/internal repo — the author check does not apply and ``trusted`` is
    forced True (the user owns access control). On a PUBLIC repo exactly one of
    ``trusted`` / ``untrusted`` is True.
    """

    trusted: bool
    untrusted: bool
    internal_repo: bool


def trusted_handles() -> set[str]:
    """The union of trusted handles (lower-cased), DB-first with config fallback.

    DB :class:`TrustedIdentity` rows are canonical. The configured
    ``user_identity_aliases`` is the FALLBACK behind this one seam (not a
    parallel source) for the windows where the canonical table cannot answer:
    an EMPTY table (pre-seed); a pre-migration ``OperationalError`` /
    ``ProgrammingError`` (no such table / relation does not exist — sibling of
    :class:`SlackBroadcastsScanner`'s pre-migration tolerance); or the canonical
    DB not being reachable from this process at all (early bootstrap / a backend
    built outside a DB context). So nothing regresses during the config-to-DB
    migration window.
    """
    try:
        trusted_identity_model = apps.get_model("core", "TrustedIdentity")

        handles = trusted_identity_model.objects.trusted_handles()
    except (OperationalError, ProgrammingError):
        logger.info("author_trust: teatree_trusted_identity unavailable (DB not migrated yet) — config fallback")
        return _config_trusted_handles()
    except RuntimeError:
        # The canonical DB is not reachable from this process (no connection /
        # access blocked). Documented fallback rather than a hard failure.
        logger.info("author_trust: canonical DB not reachable — config fallback")
        return _config_trusted_handles()
    return handles or _config_trusted_handles()


def _config_trusted_handles() -> set[str]:
    """The configured ``user_identity_aliases`` set (lower-cased), or empty.

    ``user_identity_aliases`` is DB-home (#1775), resolved through
    ``get_effective_settings`` so this fallback agrees with every other
    consumer of the alias set (the loop scanners' ``backend.identities``).
    """
    try:
        from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: call-time import, kept lazy

        aliases = get_effective_settings().user_identity_aliases
        return {alias.strip().lower() for alias in aliases if alias.strip()}
    except Exception:
        logger.exception("author_trust: failed to read user_identity_aliases fallback")
        return set()


def is_trusted_author(author: str) -> bool:
    """True iff *author* is in the trusted set (case-insensitive)."""
    cleaned = author.strip().lower()
    return bool(cleaned) and cleaned in trusted_handles()


def repo_is_internal(slug: str, *, host_kind: str = "github") -> bool:
    """True iff *slug* resolves to a PRIVATE / internal repo (no author check).

    Offline allowlist first, then the day-cached live probe. An UNRESOLVABLE
    visibility resolves to PUBLIC (returns False) — the safe direction for the
    author gate: an unknown repo requires trust.

    The merge keystone passes a bare ``owner/repo`` slug plus the ``host_kind``
    transport switch; :func:`probe_visibility` instead infers the forge from a
    host-prefixed slug. A GitLab slug is therefore host-prefixed here so the
    probe routes to ``glab`` rather than mis-routing to ``gh``.
    """
    from teatree.hooks._repo_visibility import (  # noqa: PLC0415 — deferred: call-time import, kept lazy
        slug_is_allowlisted_private,
        slug_is_private,
    )

    probe_slug = _host_prefixed_slug(slug, host_kind=host_kind)
    if slug_is_allowlisted_private(probe_slug, None):
        return True
    return slug_is_private(probe_slug)


def _host_prefixed_slug(slug: str, *, host_kind: str) -> str:
    """Prefix a bare ``group/repo`` GitLab slug with a ``gitlab`` host so the probe routes to ``glab``."""
    first_segment_has_host = "/" in slug and "." in slug.split("/", 1)[0]
    if host_kind == "gitlab" and not first_segment_has_host:
        return f"gitlab/{slug}"
    return slug


def classify_author(slug: str, author: str, *, host_kind: str = "github") -> AuthorClassification:
    """Classify *author* on *slug* — the one decision the four scanners share.

    A PRIVATE / internal repo yields ``internal_repo=True`` + ``trusted=True``
    (no author check; the user owns access control). A PUBLIC repo with a
    trusted author yields ``trusted=True``; a PUBLIC repo with an untrusted /
    unknown / empty author yields ``untrusted=True`` (fail-closed).
    """
    if repo_is_internal(slug, host_kind=host_kind):
        return AuthorClassification(trusted=True, untrusted=False, internal_repo=True)
    if is_trusted_author(author):
        return AuthorClassification(trusted=True, untrusted=False, internal_repo=False)
    return AuthorClassification(trusted=False, untrusted=True, internal_repo=False)
