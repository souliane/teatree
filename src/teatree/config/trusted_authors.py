"""The effective trusted-issue-author resolver — the config half of the trust UNION (#3235).

The issue-implementer intakes an issue on the strength of WHO AUTHORED IT, not a
hand-applied label: the owner will not tag tickets, so the author IS the authority.
That makes the trusted-author set a safety boundary — on a public repo it is the
only thing between a stranger's issue and the autonomous factory — so it is resolved
as an explicit UNION of three named sources, never a fallback chain:

1. ``user_identity_aliases`` — the owner's own handles across forges (#976).
2. ``trusted_issue_authors`` — the allowlist of OTHER humans whose issues the
    factory may act on (a colleague, an operator account).
3. The ``TrustedIdentity`` rows — the canonical DB trust table (#1773).

This module owns the union of (1) and (2). Source (3) lives in the DB, which :mod:`teatree.config`
must not reach (config is the bottom of the dependency order), so it is unioned in
one layer up at the DB-aware :mod:`teatree.core.review.author_trust` seam — the same
``classify_author`` the merge keystone and the reviewing scanners consume, so the
issue-implementer's trust decision cannot drift from theirs.

Fail-closed: an unconfigured deployment resolves to the EMPTY set. Empty means NO
issue is ever intaken on author trust — never "trust everyone".
"""

from teatree.config.settings import UserSettings


def effective_trusted_issue_authors(settings: UserSettings) -> frozenset[str]:
    """The config-tier trusted-author set for issue intake — lower-cased, blanks dropped.

    The UNION of the owner's ``user_identity_aliases`` and the ``trusted_issue_authors``
    allowlist. Handles are normalised to lower case because forge handles are
    case-insensitive and a case-sensitive gate would be trivially dodgeable; blank
    entries are dropped so a stray ``""`` in the config can never read as a wildcard
    that matches an author the forge failed to report.
    """
    configured = (*settings.user_identity_aliases, *settings.trusted_issue_authors)
    return frozenset(handle.strip().lower() for handle in configured if handle.strip())
