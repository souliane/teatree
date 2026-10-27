"""Typed worktree fields stored beside the Worktree model."""

from typing import TYPE_CHECKING, TypedDict

from teatree.core.models.checkout_proof import (
    AutoAttestation,
    CheckoutCustody,
    DeferredCheckoutExit,
    DeferredExitAttestation,
    ReleasedPrHeadAttestation,
    SafeCheckoutAttestation,
)

if TYPE_CHECKING:
    from teatree.core.provision.provision_report import ProvisionReportDict


class WorktreeExtra(TypedDict, total=False):
    worktree_path: str
    attested_state: str
    direct_attested_state: str
    direct_attested_at: str
    checkout_custody: CheckoutCustody
    auto_attestation: AutoAttestation | DeferredExitAttestation | ReleasedPrHeadAttestation | SafeCheckoutAttestation
    deferred_exit: DeferredCheckoutExit
    clone_path: str
    services: list[str]
    urls: dict[str, str]
    pids: dict[str, int]
    failed_services: list[str]
    db_refreshed_at: str
    db_import_failures: int
    setup_hook: str
    # Written by the start runner alongside ``services`` in one locked update, and
    # by the provision runner at the end of a provision. Both were absent from this
    # TypedDict, so ``_WORKTREE_EXTRA_KEYS`` filtered them straight back out of
    # every ``get_extra()`` read — declared here so the accessor keeps them.
    ports: dict[str, int]
    provision_report: "ProvisionReportDict"
    # #2227 Explicit operator pin: when true the idle-stack reaper never reaps
    # this worktree, regardless of idleness — the manual escape hatch alongside
    # the active-delivery-lease and recent-E2E-run KEEP guards.
    reaper_pinned: bool
