"""Who counts as the INDEPENDENT checker — the maker≠checker identity primitives.

``ReviewVerdict`` / ``MergeClear`` / ``Rubric`` / ``CriticVerdict`` / ``ReviewEvidence``
and the waiver surfaces (``DbApproval`` / ``E2EBypassApproval`` / ``ReproWaiver`` /
``OnBehalfApproval``) all ask the same question of a free-text identity: is this an
independent checker, or the maker attesting its own work (§17.8 clause 3)? They share
this ONE answer so they cannot drift apart.

The answer is POSITIVE identification with a fail-CLOSED default (#4241). The previous
denylist-only shape admitted every identity it did not enumerate, so a maker had only to
pick an unlisted name — and the review-authoring identities a self-implementing review
pass carries (``ac-reviewing-codebase``, ``architectural_review``) were among the ones
that passed. :func:`is_non_reviewer_role` survives as the overriding refusal;
:func:`is_independent_reviewer_identity` is what decides admission.

This module is a self-governance seam: it DEFINES the trust boundary that judges the
factory's own merges, so its path is listed in ``merge_clear._SUBSTRATE_PATH_PREFIXES``
and a change to it can never auto-merge on agent-only review.
"""

import re
from typing import Literal

# §17.8 clause 3 / §17.6 candidate 13: an independent cold-review attestation
# cannot be recorded by the maker/coding-agent/loop side — the author would be
# rubber-stamping their own work. Every guard that asks the question shares this
# single list so they cannot drift apart.
#
# Punctuated-prefix tokens ("maker:", "maker-", "coding-agent") are matched as
# leading prefixes; bare role words are matched anywhere by :data:`_MAKER_ROLE_WORDS`.
NON_REVIEWER_AGENT_PREFIXES = ("maker:", "maker-", "coding-agent", "coding", "loop")

# The independent checker is identified POSITIVELY, not by absence from the maker list
# above (#4241). A recognised reviewer role appears as a delimited component of the
# identity, so the factory's own spellings — "cold-reviewer", "t3:reviewer",
# "orchestrator-cold-review", "cr/prd" — all resolve without enumerating each one.
# Bare "review" / "reviewing" are deliberately NOT admitting tokens: they are exactly
# what the review-AUTHORING maker roles carry ("architectural_review",
# "ac-reviewing-codebase" — the identity a review pass that implements its own findings
# and opens a PR would naturally use), and admitting them would re-open the hole.
# "codex" is admitted because `PrReviewBackend` names it as a REVIEW backend; "claude"
# is not, because the same model is also the maker.
REVIEWER_ROLE_COMPONENTS = frozenset({"reviewer", "cold", "cr", "critic", "adjudicator", "checker", "codex"})


_IDENTITY_DELIMITERS = re.compile(r"[^a-z0-9]+")

# A maker ROLE word names who the identity is, so it refuses unconditionally.
_MAKER_ROLE_WORDS = frozenset({"maker", "coding", "loop"})

#: What a dispatched reviewer is TOLD to return. Derived from the two sets above so the
#: instruction cannot drift from the gate, and it states BOTH constraints: a positive one
#: (44 of 51 recorded refusals named no admitting token) and the negative (7 more carried
#: one but were refused for a maker word). The example is concrete because a model copies
#: an example over prose, and a bare token would collide on the per-head unique index.
REVIEWER_IDENTITY_INSTRUCTION = (
    "cold-reviewer-<pr-or-task-id>"
    " (must contain one of " + "/".join(sorted(REVIEWER_ROLE_COMPONENTS)) + ";"
    " never " + "/".join(sorted(_MAKER_ROLE_WORDS)) + ")"
)

# A review PHASE word names what the identity is doing, and the periodic holistic pass
# does it while implementing its own findings and opening its own PR (#4230) — a MAKER
# wearing a review word. It refuses only when nothing else in the identity names a
# reviewer, so "cold-reviewer-4152-reviewing" stays a reviewer while
# "cold-architectural-review" does not: "cold" qualifies a reviewer, it never names one.
_REVIEW_PHASE_WORDS = frozenset({"reviewing", "architectural"})
_WEAK_REVIEWER_MODIFIERS = frozenset({"cold"})

# The review-AUTHORING agents by NAME, matched as a contiguous component run so no
# decoration neutralises them (#4408). A phase word alone is neutralised by any reviewer
# noun, and no token-presence rule can separate "ac-reviewing-codebase-reviewer" from the
# legitimate "cold-reviewer-4152-reviewing" — both carry a phase word and a reviewer noun.
# Naming the maker outright is what closes it, and keeping the list to the identities the
# holistic pass actually carries is what keeps it from re-opening #4241's over-refusals.
_REVIEW_AUTHORING_IDENTITIES: tuple[tuple[str, ...], ...] = (
    ("ac", "reviewing", "codebase"),
    ("architectural", "review"),
)

#: Which rule refused the identity — the message explains each one differently (#4408).
RefusalClass = Literal["maker-role", "review-authoring"]


def _identity_components(identity: str) -> tuple[str, ...]:
    """*identity* split into its delimited components, in order, blanks dropped."""
    return tuple(str(part) for part in _IDENTITY_DELIMITERS.split(normalize_reviewer_identity(identity)) if part)


def _names_a_review_authoring_identity(components: tuple[str, ...]) -> bool:
    return any(
        components[start : start + len(name)] == name
        for name in _REVIEW_AUTHORING_IDENTITIES
        for start in range(len(components) - len(name) + 1)
    )


def _refusal_class(identity: str) -> RefusalClass | None:
    """Which overriding refusal *identity* trips, or ``None`` when it trips none.

    Punctuated-prefix tokens ("maker:", "maker-", "coding-agent") are matched as leading
    prefixes. Bare role words are matched as any delimited component, splitting on every
    run of non-alphanumerics, so neither the executor's canonical "merge-loop" nor a
    re-spelling under an exotic delimiter ("merge loop", "team.maker/x") escapes — a
    narrower split let a space buy admission (#4378). Incidental substrings ("decoding")
    are not matched because the split honours delimiters only.
    """
    lowered = normalize_reviewer_identity(identity)
    if any(lowered == prefix or lowered.startswith(prefix) for prefix in NON_REVIEWER_AGENT_PREFIXES):
        return "maker-role"
    components = _identity_components(lowered)
    parts = frozenset(components)
    if parts & _MAKER_ROLE_WORDS:
        return "maker-role"
    if _names_a_review_authoring_identity(components):
        return "review-authoring"
    if parts & _REVIEW_PHASE_WORDS and not (parts & (REVIEWER_ROLE_COMPONENTS - _WEAK_REVIEWER_MODIFIERS)):
        return "review-authoring"
    return None


def is_non_reviewer_role(identity: str) -> bool:
    """True iff ``identity`` is a maker/coding-agent/loop/review-authoring role (§17.8 clause 3).

    This is the OVERRIDING refusal, no longer the whole test: admission is decided
    positively by :func:`is_independent_reviewer_identity` (#4241). It shares
    :func:`_refusal_class` with :func:`unrecognised_reviewer_message` so the refusal and
    the explanation of it can never disagree.
    """
    return _refusal_class(identity) is not None


def _configured_reviewer_identities() -> frozenset[str]:
    """The deployment's extra reviewer allowlist; an unreadable config resolves EMPTY.

    Empty is the fail-CLOSED direction here — admission falls back to the role-token
    harness rather than widening — so a config read that raises must not be allowed to
    propagate out of a merge-time guard and turn a refusal into a crash.
    """
    from teatree.config import (  # noqa: PLC0415 — deferred: config read at call time, never import scope
        effective_independent_reviewer_identities,
        get_effective_settings,
    )

    try:
        return effective_independent_reviewer_identities(get_effective_settings())
    except Exception:  # noqa: BLE001 — any config failure degrades to the fail-closed empty set
        return frozenset()


def is_independent_reviewer_identity(identity: str) -> bool:
    """True iff *identity* positively identifies an independent checker (#4241, §17.8 clause 3).

    The inverse of :func:`is_non_reviewer_role`, and NOT merely its negation. The
    denylist admitted every identity it did not enumerate, so a maker only had to pick
    an unlisted name — the guarantee was stated as mechanical and was in fact
    conventional. Admission here is positive and the default is refusal:

    1. a maker/coding-agent/loop/review-authoring role is refused outright, so the
        denylist keeps overriding whatever else the identity carries;
    2. a delimited component naming a recognised reviewer role
        (:data:`REVIEWER_ROLE_COMPONENTS`) admits — this is every agent reviewer, and it
        is checked before the config read so the hot path touches no settings;
    3. an identity the deployment configured as a reviewer (the owner's own handles,
        plus ``independent_reviewer_identities``) admits — this is how a human, whose
        handle carries no role word, records a verdict;
    4. anything else — including a novel identity nobody has seen before — is refused.

    This narrows WHO may attest; it does not touch WHAT is attested. The head-SHA bind,
    the staleness rule and the always-on fail-closed posture of
    :func:`~teatree.core.merge.authorization.assert_review_verdict_gate` are unchanged.
    """
    candidate = normalize_reviewer_identity(identity)
    if not candidate or is_non_reviewer_role(candidate):
        return False
    components: frozenset[str] = frozenset(str(part) for part in _IDENTITY_DELIMITERS.split(candidate) if part)
    return bool(components & REVIEWER_ROLE_COMPONENTS) or candidate in _configured_reviewer_identities()


_REFUSAL_REASONS: dict[RefusalClass, str] = {
    "maker-role": "is a maker/coding-agent/loop non-reviewer role",
    "review-authoring": (
        "is a review-AUTHORING non-reviewer role: it implements its own findings and opens its own PR (#4230)"
    ),
}
_UNRECOGNISED_REASON = "names no recognised reviewer role, so it is refused fail-closed (#4241)"

_OVERRIDING_REMEDY = (
    "This refusal overrides the `independent_reviewer_identities` allowlist and survives any renaming, "
    "so the remedy is a review by someone else."
)
_CONFIGURE_REMEDY = (
    "If it is a human or an external reviewer, admit it on the record with "
    "`t3 <overlay> config_setting set independent_reviewer_identities '[\"<id>\"]'`."
)


def unrecognised_reviewer_message(identity: str, *, subject: str, verb: str) -> str:
    """The shared refusal text for an identity that is not an independent checker.

    Every chokepoint that rejects through :func:`is_independent_reviewer_identity` uses
    this, so a fail-CLOSED gate always names its own escape: a refusal an operator
    cannot act on is the way a safety gate gets disabled wholesale instead of
    configured. *subject* / *verb* localise it to the artifact ("a CLEAR", "issued").

    The escape it names is the RECORDED one, never a token to append (#4408). Enumerating
    :data:`REVIEWER_ROLE_COMPONENTS` here handed every phase-word refusal its own bypass:
    appending any listed token admitted the identity that had just been refused, leaving
    no trace, so the refusal published a self-service exemption. The message now says what
    an independent reviewer IS, and names the config allowlist only for the class the
    allowlist can actually admit.
    """
    refusal = _refusal_class(identity)
    reason = _REFUSAL_REASONS[refusal] if refusal else _UNRECOGNISED_REASON
    remedy = _OVERRIDING_REMEDY if refusal else _CONFIGURE_REMEDY
    return (
        f"identity {identity!r} {reason} — {subject} is {verb} by an independent cold "
        f"reviewer, never self-attested (§17.8 clause 3): an independent reviewer is a different "
        f"agent or human from the one that authored the change under review. {remedy}"
    )


def normalize_reviewer_identity(identity: str) -> str:
    """The canonical, idempotency-keyed form of a free-text reviewer identity (F8).

    The recorded ``reviewer_identity`` is free text — 187 distinct values on the
    live box, with the SAME logical reviewer spelled many ways ("Codex", "codex ",
    "codex"). That made "has this sha been reviewed by this identity?" unanswerable
    by query and let the dispatcher re-review one sha 17 times. The normalized form
    collapses the case-and-whitespace noise only — ``strip`` + internal-whitespace
    runs to one space + ``casefold`` — so equivalent spellings key to ONE row while
    genuinely distinct identities stay distinct (no role-prefix stripping, which
    would over-merge two real reviewers). It is the canonical key at every boundary:
    the ``ReviewVerdict`` write, its uniqueness constraint, and the maker≠checker
    comparison at CLEAR issuance and at merge time.

    It sits beside :func:`is_non_reviewer_role` rather than next to ``ReviewVerdict``
    because CLEAR issuance validates through it and ``review_verdict`` imports this
    module — the reviewer-identity primitives are the shared lower layer.
    """
    return " ".join(identity.split()).casefold()
