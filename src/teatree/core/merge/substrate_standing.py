"""The standing substrate-merge authorization — ONE policy for both merge paths (#3648).

A substrate change is held for the owner by default (#2727). Two standing owner
opt-ins lift that hold, and both the keystone/CLEAR path
(:mod:`teatree.core.merge.authorization`) and the solo-overlay ``pr_sweep``
bypass (:mod:`teatree.loop.scanners.pr_sweep_substrate`) resolve them here, so
the two can never disagree about the same PR under the same configuration.

The hold is a blast-radius sign-off, never a quality gate: lifting it changes
only WHO authorizes the substrate merge. The floor below it — an independent
cold-review verdict bound to the live head, reviewed-SHA bind, CI-green,
not-draft, maker≠checker, anti-vacuity — runs unchanged on both paths.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SubstrateStandingAuthorization:
    """Which standing opt-in (if any) authorizes a substrate merge on an overlay (#3648).

    ``delegated_by`` carries the config-sourced owner id when the standing
    delegation ``substrate_auto_merge_authorized_by`` authorizes the merge (the
    presented id still equals the configured value) — the id stamped onto the
    ``MergeAudit`` row. ``self_signoff`` is the explicit, default-off
    ``substrate_self_signoff`` grant on a ``full``-autonomy overlay. Truthy iff
    either opt-in authorizes, so a caller that only needs the yes/no reads
    ``bool(...)``.
    """

    delegated_by: str = ""
    self_signoff: bool = False

    def __bool__(self) -> bool:
        return bool(self.delegated_by) or self.self_signoff


def substrate_standing_authorization(
    *, overlay_name: str, presented_authorizer: str = ""
) -> SubstrateStandingAuthorization:
    """The ONE substrate-authorization policy decision, shared by both merge paths (#3648).

    The keystone/CLEAR path reaches it through
    :func:`_config_standing_substrate_delegation` /
    :func:`_overlay_grants_standing_substrate_signoff`; the solo-overlay
    ``pr_sweep`` bypass reaches it through
    :func:`teatree.loop.scanners.pr_sweep_substrate.solo_overlay_substrate_authorized`.
    Both read the same two owner opt-ins off the same overlay, so the two paths
    can never disagree about the same PR — the divergence #3648 reports.

    This governs only the blast-radius sign-off. The quality/safety floor
    (independent cold review bound to the live head, reviewed-SHA bind, CI-green,
    not-draft, maker≠checker, anti-vacuity) is unaffected and still runs on both
    paths.
    """
    from teatree.config import Autonomy, get_effective_settings  # noqa: PLC0415 — deferred: call-time import, kept lazy

    name = overlay_name.strip()
    if not name:
        return SubstrateStandingAuthorization()
    settings = get_effective_settings(overlay_name=name)
    configured = settings.substrate_auto_merge_authorized_by.strip()
    presented = presented_authorizer.strip()
    return SubstrateStandingAuthorization(
        delegated_by=configured if configured and presented == configured else "",
        self_signoff=settings.substrate_self_signoff and settings.autonomy is Autonomy.FULL,
    )


def resolve_overlay_by_repo_identity(*slugs: str, fallback: str = "") -> str:
    """The overlay owning the first *slugs* entry that resolves, else *fallback*.

    Repo identity (:func:`infer_overlay_for_url` over every overlay's
    ``get_workspace_repos``) is authoritative; *fallback* is the stored overlay
    token, used only when no slug resolves — see :func:`_resolve_clear_overlay_name`
    for why the token is the last resort.
    """
    from teatree.core.overlay_loader import infer_overlay_for_url  # noqa: PLC0415 — deferred: call-time import

    for slug in slugs:
        inferred = infer_overlay_for_url(slug.strip()).strip()
        if inferred:
            return inferred
    return fallback.strip()
