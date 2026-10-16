"""Typed records for checkout custody and attestation."""

from typing import Literal, TypedDict


class CheckoutTenure(TypedDict):
    holder: str
    session: str
    since: str
    ended: str
    ended_at: str


class CheckoutCustody(TypedDict):
    anchor: str
    tenures: list[CheckoutTenure]


class CheckoutProcessEvidence(TypedDict):
    source: str
    gaps: list[str]


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
    holder: CheckoutHolderEvidence


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
