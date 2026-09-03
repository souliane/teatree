"""Whether this deployment knows the identities it must act as and be reviewed by (#4241 follow-up).

Two identity facts decide whether the factory can ship at all, and both fail SILENTLY:

* **who reviews.** A CLEAR is admitted from an identity naming a reviewer role, or from one this
    deployment configured (``user_identity_aliases`` plus ``independent_reviewer_identities``,
    resolved by :func:`~teatree.config.reviewer_identities.effective_independent_reviewer_identities`).
    With both empty, a human owner's own handle names no role token and is refused, so the merge
    keystone admits agent identities only and a human-approved merge is impossible.
* **who authors.** An overlay may write one repo under a bot credential so the owner stays eligible
    to approve it (:meth:`~teatree.core.overlay.OverlayConfig.acts_as_distinct_identity_on`).
    Reading that as a boolean collapses "the bot credential resolved" with "it did not", so a
    deployment that cannot reach its bot is indistinguishable from one that never had one.

Neither surfaces until a merge is refused or somebody reads ``author.username`` on an MR already
open. Both questions are asked here so ``t3 doctor check`` fails on either.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

#: Consumers that degrade to their empty-set behaviour when no owner handle is configured. Named
#: in the remedy because "the list is empty" reads as one missing feature rather than eleven.
OWNER_IDENTITY_CONSUMERS: tuple[str, ...] = (
    "the merge keystone (a human CLEAR is refused)",
    "issue intake (no author is trusted)",
    "the owner's own-MR reply carve-out",
    "the colleague-branch cleanup guard",
    "cross-forge MR reminders",
    "the forge PR-budget gate",
)


@dataclass(frozen=True, slots=True)
class IdentityFault:
    """One identity the deployment cannot resolve, with the command that resolves it."""

    summary: str
    remedy: str

    def lines(self) -> tuple[str, str]:
        return (f"FAIL  {self.summary}", f"      Fix: {self.remedy}")


class AuthoringIdentity(StrEnum):
    """Whose credential a remote's MRs are actually written under.

    The three-valued answer :meth:`OverlayConfig.acts_as_distinct_identity_on` cannot give.
    ``UNRESOLVABLE`` is the one this exists for: a scoped credential that did not resolve while
    the owner's did means the overlay INTENDS a distinct author and this venue cannot be one.
    """

    DISTINCT = "distinct"
    OWNER = "owner"
    UNRESOLVABLE = "unresolvable"


def classify_authoring_identity(*, owner_token: str, scoped_token: str) -> AuthoringIdentity:
    """Which identity *scoped_token* writes as, relative to the overlay-wide *owner_token*.

    An empty scoped credential beside a resolvable owner one is ``UNRESOLVABLE`` — the overlay
    routed this remote somewhere and the store answered nothing. Both empty is ``OWNER``: nothing
    is configured anywhere, so there is no distinct author to have lost.
    """
    if scoped_token == owner_token:
        return AuthoringIdentity.OWNER
    return AuthoringIdentity.DISTINCT if scoped_token else AuthoringIdentity.UNRESOLVABLE


def owner_identity_fault(configured: Iterable[str]) -> IdentityFault | None:
    """The fault when this deployment has configured no admissible reviewer identity.

    *configured* is the already-resolved effective allowlist, so the judgement stays pure and the
    doctor wrapper is left with only the config read. A blank-only list is empty: the resolver
    drops blanks precisely so a stray ``""`` can never read as a wildcard.
    """
    if any(entry.strip() for entry in configured):
        return None
    return IdentityFault(
        summary=(
            "No forge identity is admissible as an independent reviewer — `user_identity_aliases` "
            "and `independent_reviewer_identities` are both empty, so the merge keystone refuses "
            "every human CLEAR and " + ", ".join(OWNER_IDENTITY_CONSUMERS[1:]) + " all run degraded."
        ),
        remedy=(
            "`t3 identities bootstrap` derives the owner's handles from the forge logins this "
            "venue authenticates as, excluding any declared in `self_forge_identities`"
        ),
    )


def authoring_identity_fault(*, remote: str, identity: AuthoringIdentity) -> IdentityFault | None:
    """The fault when a remote's declared distinct author cannot be acted as from this venue.

    Only ``UNRESOLVABLE`` is a fault. ``OWNER`` is the ordinary repo the owner authors himself, and
    a permanently-red check on it is how a doctor check gets ignored.
    """
    if identity is not AuthoringIdentity.UNRESOLVABLE:
        return None
    return IdentityFault(
        summary=(
            f"{remote} is declared to be authored under a non-owner credential, but that credential "
            f"does not resolve here — MRs will be opened by the owner, who then cannot approve them."
        ),
        remedy=(
            "provision the overlay's scoped forge credential in this venue's secret store (a "
            "container seeds its own store at start-up and must seed this entry too)"
        ),
    )


def derivable_owner_identities(*, forge_logins: Iterable[str], self_identities: Iterable[str]) -> tuple[str, ...]:
    """The logins this venue authenticates as MINUS every login it also ACTS as.

    The exclusion is what keeps the derivation safe to run unattended. A deployed factory
    authenticates its forge client as its own bot, so a bootstrap that trusted the ambient login
    would write that bot into the owner allowlist and hand a coding agent an identity the merge
    keystone admits — self-attestation, arrived at by configuration rather than by code. Declared
    self-identities are subtracted across every host: a bot login is a bot login wherever it is
    declared, and there is no owner whose handle is also one we act as.

    Order is preserved and duplicates collapse, so the written list reads the way the operator's
    forges are configured rather than in set order.
    """
    excluded = {entry.strip().casefold() for entry in self_identities if entry.strip()}
    derived: list[str] = []
    for login in forge_logins:
        cleaned = login.strip()
        if not cleaned or cleaned.casefold() in excluded or cleaned in derived:
            continue
        derived.append(cleaned)
    return tuple(derived)
