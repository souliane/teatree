"""Trust provenance of inbound content — the taint key the context firewall stamps (#116).

The lethal-trifecta mitigation classifies WHERE a piece of content came from at the
single ingestion boundary, then carries that classification as a taint through the
directive FSM so an untrusted-origin action can never AUTO_APPROVE (the floor in
:mod:`teatree.core.models.approval_policy`). :class:`Provenance` is the closed vocabulary;
:func:`classify_provenance` is the pure classifier the webhook persist chokepoint
consults.

Fail-closed: every provenance except :attr:`Provenance.OWNER` is untrusted, and an
unrecognised / unstamped origin resolves to :attr:`Provenance.PUBLIC` (the most
untrusted) — a missing classification never reads as trusted.
"""

from enum import StrEnum

from teatree.core.models.trusted_identity import TrustedIdentity


class Provenance(StrEnum):
    """Where inbound content originated, ordered by trust — only ``OWNER`` is trusted.

    ``OWNER`` — one of the operator's own identities (the ``TrustedIdentity`` set).
    ``TRUSTED_COLLEAGUE`` — a registered colleague; still leg B of the trifecta (an
    account can be compromised), so floored to ASK exactly like ``PUBLIC``.
    ``PUBLIC`` — anyone else / an unstamped row (fail-closed default).
    ``WEB`` — a scanned-news / crawled origin.
    """

    OWNER = "owner"
    TRUSTED_COLLEAGUE = "trusted_colleague"
    PUBLIC = "public"
    WEB = "web"


def classify_provenance(source: str, actor: str) -> Provenance:
    """Classify an inbound event's trust from its ``(source, actor)`` — a pure verdict.

    ``OWNER`` iff *actor* matches one of the operator's ``TrustedIdentity`` handles
    (case-insensitive, platform-tolerant); every other actor — a colleague, a public
    stranger, a blank actor — resolves to ``PUBLIC`` (fail-closed).

    In #116 this emits only ``OWNER`` / ``PUBLIC``: there is no separate colleague
    registry and no crawled-web ingestion path yet. ``TRUSTED_COLLEAGUE`` / ``WEB``
    exist in the enum for the floor's UNTRUSTED_TAINTS semantics (all non-``OWNER``
    taints are floored to ASK identically); a future colleague-registry / news-scanner
    ingestion path wires them here without touching the floor.
    """
    if actor.strip() and TrustedIdentity.objects.is_trusted(actor, platform=source):
        return Provenance.OWNER
    return Provenance.PUBLIC
