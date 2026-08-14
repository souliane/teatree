"""The config tier of the independent-checker allowlist (#4241).

maker≠checker is enforced against a free-text ``reviewer_identity``. The harness
recognises a reviewer by ROLE (``teatree.core.models.merge_clear.REVIEWER_ROLE_COMPONENTS``),
which covers the factory's own agents but not a human, whose handle carries no role word.
This module is the data half: the identities THIS deployment additionally trusts as
independent checkers, resolved as an explicit UNION of two named sources.

1. ``user_identity_aliases`` — the owner's own handles across forges (#976). A human
    owner recording a verdict is the strongest independent review there is.
2. ``independent_reviewer_identities`` — the explicit allowlist for anything else (a
    colleague, an external review service, a novel agent role). It is the no-code-change
    escape a fail-CLOSED gate needs: without it, a reviewer whose name carries no
    recognised role token can only be admitted by shipping a new release.

Fail-closed: an unconfigured deployment resolves to the EMPTY set, so admission falls
back to the role-token harness alone — never to "trust every unrecognised identity",
which is precisely the pre-#4241 behaviour this replaces.
"""

from teatree.config.settings import UserSettings


def effective_independent_reviewer_identities(settings: UserSettings) -> frozenset[str]:
    """The config-tier extra reviewer allowlist, keyed the way verdicts are keyed.

    Entries are canonicalized through the same strip + whitespace-collapse + casefold
    ``normalize_reviewer_identity`` applies to a recorded ``reviewer_identity``, so a
    configured ``"Souliane"`` matches a verdict recorded as ``"souliane "``. Blank
    entries are dropped so a stray ``""`` in the config can never read as a wildcard
    that matches an identity the caller failed to supply.
    """
    configured = (*settings.user_identity_aliases, *settings.independent_reviewer_identities)
    return frozenset(" ".join(entry.split()).casefold() for entry in configured if entry.strip())
