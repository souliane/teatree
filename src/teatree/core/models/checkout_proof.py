"""Typed records for checkout custody and attestation."""

from typing import Literal, NotRequired, TypedDict


class CheckoutTenure(TypedDict):
    holder: str
    session: str
    since: str
    ended: str
    ended_at: str
    namespace: NotRequired[str]


class CheckoutCustody(TypedDict):
    anchor: str
    tenures: list[CheckoutTenure]


class CheckoutProcessEvidence(TypedDict):
    source: str
    gaps: list[str]
    set_aside: NotRequired[list[str]]


class CheckoutHolderEvidence(TypedDict):
    id: str
    session: str


class AutoAttestation(TypedDict):
    at: str
    previous: str
    digest: str
    tenures: list[CheckoutTenure]
    process_table: CheckoutProcessEvidence
    branches: dict[str, str]
    pull_requests: dict[str, str]
    holder: CheckoutHolderEvidence


class ReleasedPrHeadAttestation(TypedDict):
    at: str
    previous: str
    digest: str
    anchor_commit: str
    current_head: str
    pr_head: str
    remote_head: str
    pull_request: str
    tenures: list[CheckoutTenure]
    process_table: CheckoutProcessEvidence
    holder: CheckoutHolderEvidence


class SafeCheckoutPreservation(TypedDict):
    commits: int
    artifact_prefix: str


class SafeCheckoutAttestation(TypedDict):
    at: str
    digest: str
    branch: str
    process_evidence: Literal["host", "venue"]
    process_table: CheckoutProcessEvidence
    holder: CheckoutHolderEvidence
    preserved: SafeCheckoutPreservation


class DeferredCheckoutExit(TypedDict):
    holder: str
    session: str
    digest: str
    cause: Literal["own_processes", "process_table"]
    at: str


class DeferredExitAttestation(TypedDict):
    at: str
    previous: str
    digest: str
    tenures: list[CheckoutTenure]
    process_table: CheckoutProcessEvidence
    holder: CheckoutHolderEvidence
    deferred_exit: DeferredCheckoutExit
