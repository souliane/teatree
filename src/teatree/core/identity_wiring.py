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

from collections.abc import Iterable, Mapping
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


def unapprovable_author_fault(*, remote: str, authenticated: str, approvers: Iterable[str]) -> IdentityFault | None:
    """The fault when the credential about to author *remote* is one the approver set relies on.

    A forge bars an MR's author from approving it, so a repo whose MRs only the owner can approve
    must not be written under the owner's own credential. Read back BEFORE the create, because
    afterwards the only remedy is to close the MR and open it again.

    An unreadable identity on such a repo is a fault too: the author cannot be proved right, and
    the cost of refusing is one retry against an MR nobody can approve.
    """
    if not authenticated.strip():
        return IdentityFault(
            summary=(
                f"could not read back which identity would author MRs on {remote}, which is declared "
                f"to be authored under a non-owner credential — refusing rather than opening an MR "
                f"that may turn out to be unapprovable"
            ),
            remedy=(
                "check the scoped forge credential is readable from this venue "
                "(`t3 doctor check` reports the same fault), then retry"
            ),
        )
    normalized = " ".join(authenticated.split()).casefold()
    if normalized not in {" ".join(entry.split()).casefold() for entry in approvers if entry.strip()}:
        return None
    return IdentityFault(
        summary=(
            f"{remote} would be authored by {authenticated!r}, who is also this deployment's approver "
            f"— a forge refuses an approval from an MR's own author, so the MR would be unapprovable"
        ),
        remedy=(
            "provision the repo's declared non-owner forge credential in this venue's secret store, "
            "or drop the identity from the approver allowlist so somebody else can approve"
        ),
    )


def unapprovable_open_mr_fault(
    *, remote: str, authors_by_ref: Mapping[str, str], approvers: Iterable[str]
) -> IdentityFault | None:
    """The fault when *remote* already carries OPEN MRs authored by an approver identity.

    :func:`unapprovable_author_fault` refuses such an MR before it exists; this is the
    after-the-fact half, for the ones opened before that refusal existed or through a surface it
    cannot reach (the web UI). They sit open looking healthy and answer an approval attempt with
    401 — a status indistinguishable from a dead credential — so they are worth naming here
    rather than at the moment somebody needs the merge.

    Scoped to remotes that DECLARE a non-owner author: on an ordinary repo the owner authors his
    own MRs and approves nothing, so a check over those would be permanently red.
    """
    eligible = {" ".join(entry.split()).casefold() for entry in approvers if entry.strip()}
    unapprovable = sorted(
        ref for ref, author in authors_by_ref.items() if " ".join(author.split()).casefold() in eligible
    )
    if not unapprovable:
        return None
    return IdentityFault(
        summary=(
            f"{remote} has {len(unapprovable)} OPEN merge request(s) authored by this deployment's own "
            f"approver, who a forge bars from approving them — they cannot be merged as they stand: "
            f"{', '.join(unapprovable)}"
        ),
        remedy=(
            "close each one and re-open it through `t3 <overlay> pr create <ticket-id>` (or "
            "`t3 <overlay> pr ensure-pr --repo <abs-path> --branch <branch>`), which writes under the "
            "repo's declared non-owner credential"
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
