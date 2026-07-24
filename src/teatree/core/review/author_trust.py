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
from enum import Enum

from django.apps import apps
from django.db import OperationalError, ProgrammingError

logger = logging.getLogger(__name__)


class TrustVerdict(Enum):
    """Whether the factory may act on an artifact unattended.

    Named ``TrustVerdict`` rather than ``Autonomy`` because :class:`teatree.config.Autonomy`
    already names the operator's babysit/notify/full autonomy TIER — a different axis.
    """

    AUTONOMOUS = "autonomous"
    HUMAN_REVIEW = "human_review"


class AutonomyGate(Enum):
    """The two gates the ONE trust boundary governs (#3577).

    ``INTAKE`` — may the factory turn this author's ISSUE into work?
    ``MERGE`` — may the factory auto-merge this author's PR?

    Intake is deliberately STRICTER: it additionally requires EXPLICIT membership
    of the trusted set, so the private-repo bypass (right for judging a merge on a
    repo whose access the owner controls) cannot hand an unlisted collaborator the
    keys to the autonomous factory. Merge is the one that additionally applies the
    strict fork model.
    """

    INTAKE = "intake"
    MERGE = "merge"


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


def is_trusted_author(author: str, *, extra_trusted: frozenset[str] = frozenset()) -> bool:
    """True iff *author* is in the trusted set (case-insensitive).

    *extra_trusted* is a caller-supplied set of ALREADY-normalised (lower-cased)
    handles, UNIONED with :func:`trusted_handles` rather than replacing it (#3235).
    It exists because two of the issue-implementer's three trust sources —
    ``user_identity_aliases`` and the ``trusted_issue_authors`` allowlist — live in
    :mod:`teatree.config`, which sits below the DB and so cannot union itself with
    the ``TrustedIdentity`` rows; the caller resolves those two
    (:func:`teatree.config.effective_trusted_issue_authors`) and hands them in here,
    where the DB half is known. Default-empty, so every pre-existing consumer (the
    merge keystone, the four reviewing scanners) resolves exactly as before.

    Fail-closed: an EMPTY author is never trusted, whatever *extra_trusted* holds.
    """
    cleaned = author.strip().lower()
    return bool(cleaned) and cleaned in (trusted_handles() | extra_trusted)


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


def classify_author(
    slug: str,
    author: str,
    *,
    host_kind: str = "github",
    extra_trusted: frozenset[str] = frozenset(),
) -> AuthorClassification:
    """Classify *author* on *slug* — the one decision the four scanners share.

    A PRIVATE / internal repo yields ``internal_repo=True`` + ``trusted=True``
    (no author check; the user owns access control). A PUBLIC repo with a
    trusted author yields ``trusted=True``; a PUBLIC repo with an untrusted /
    unknown / empty author yields ``untrusted=True`` (fail-closed).

    *extra_trusted* widens only the TRUST SET, never the repo-visibility rule — see
    :func:`is_trusted_author`. Callers that additionally need the author to be an
    EXPLICIT member of the trusted set (the issue-implementer's intake gate, where the
    internal-repo bypass would otherwise hand an unlisted collaborator the keys to the
    autonomous factory) must conjoin :func:`is_trusted_author`; ``trusted=True`` alone
    means "cleared for this repo", not "named in the trust set".
    """
    if repo_is_internal(slug, host_kind=host_kind):
        return AuthorClassification(trusted=True, untrusted=False, internal_repo=True)
    if is_trusted_author(author, extra_trusted=extra_trusted):
        return AuthorClassification(trusted=True, untrusted=False, internal_repo=False)
    return AuthorClassification(trusted=False, untrusted=True, internal_repo=False)


def classify_pr_provenance(
    slug: str,
    author: str,
    *,
    same_repo: bool | None,
    host_kind: str = "github",
    extra_trusted: frozenset[str] = frozenset(),
) -> AuthorClassification:
    """Classify a merge by the PR head branch's PROVENANCE — the two merge gates' seam.

    Owner decision (BLUEPRINT §17.4.3): a PR whose head branch lives in a FORK /
    cross-repo ALWAYS requires a human, even when the author is a trusted
    identity — a fork PR is attacker-controllable code proposing itself for
    auto-merge. Provenance the forge could not report (*same_repo* is ``None`` — a
    transient read error) fails closed to the identity+visibility author check
    :func:`classify_author`.

    STRICT: ``same_repo=False`` returns ``untrusted`` regardless of author, so a
    trusted-author fork still holds for human approval. And ``same_repo=True`` is
    NOT trusted unconditionally — a same-repo head branch is still conjoined with
    the identity+visibility check (:func:`classify_author` = internal repo OR
    trusted author), so on a PUBLIC repo a push from a non-trusted push-access
    account (an added collaborator, a compromised token) still holds for human
    approval instead of auto-merging. Only the two MERGE gates (the sweep rung +
    the keystone) adopt this; :func:`classify_author`'s other consumers (the
    reviewing scanners, the issue-implementer) keep the pure identity model.
    """
    if same_repo is False:
        return AuthorClassification(trusted=False, untrusted=True, internal_repo=False)
    # same_repo True or None: still require internal-repo OR a trusted author.
    return classify_author(slug, author, host_kind=host_kind, extra_trusted=extra_trusted)


@dataclass(frozen=True, slots=True)
class AuthorSubject:
    """The artifact whose author is being judged — one bundle for both gates.

    ``same_repo`` is the PR head branch's provenance and applies only at the
    ``MERGE`` gate; an issue leaves it ``None``.
    """

    slug: str
    author: str
    host_kind: str = "github"
    same_repo: bool | None = None


def decide_author_trust(
    subject: AuthorSubject,
    *,
    gate: AutonomyGate,
    extra_trusted: frozenset[str] = frozenset(),
) -> TrustVerdict:
    """The ONE autonomy decision, applied at BOTH the intake and merge gates (#3577).

    The owner's principle: the factory works autonomously on the owner's own issues
    and PRs; an external contributor's must be carefully reviewed by a human. That
    is one trust boundary, so it gets one application over the shared resolver —
    :func:`classify_author` / :func:`classify_pr_provenance` — rather than a
    hand-rolled conjunction at each gate.

    ``INTAKE`` conjoins the repo-scoped classification with EXPLICIT trusted-set
    membership (see :class:`AutonomyGate`); ``subject.same_repo`` does not apply and
    is ignored. ``MERGE`` runs the strict fork model: a fork / cross-repo head branch
    is held for a human even from a trusted author, and unreported provenance falls
    back to the identity+visibility check.

    Fail-closed throughout: an empty / unknown author, an unresolvable repo, and a
    fork head all resolve :attr:`TrustVerdict.HUMAN_REVIEW`.
    """
    if gate is AutonomyGate.INTAKE:
        classification = classify_author(
            subject.slug, subject.author, host_kind=subject.host_kind, extra_trusted=extra_trusted
        )
        trusted = classification.trusted and is_trusted_author(subject.author, extra_trusted=extra_trusted)
    else:
        trusted = classify_pr_provenance(
            subject.slug,
            subject.author,
            same_repo=subject.same_repo,
            host_kind=subject.host_kind,
            extra_trusted=extra_trusted,
        ).trusted
    return TrustVerdict.AUTONOMOUS if trusted else TrustVerdict.HUMAN_REVIEW
