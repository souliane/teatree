"""The first-class :class:`Variant` value object (PR-27, souliane/teatree#787).

A worktree's *variant* is a per-tenant flavour of an overlay's data — its
DSLR snapshot tenant, default language, and E2E credentials all key off it.
Before this module those derivations were scattered as raw-``str`` transforms
across call sites (``get_dslr_tenant_for_variant(variant: str) -> str``), so a
tenant / snapshot / credentials name was reconstructed from a bare string in
each place that needed one — the exact drift a value object exists to stop.

:meth:`OverlayBase.resolve_variant` is now the single seam that turns a variant
*name* into a fully-resolved :class:`Variant`; no overlay hook takes a raw
``variant: str`` any more. Core reads ``variant.canonical_tenant`` /
``variant.dslr_snapshot_name`` / ``variant.e2e_credentials_key`` off the
resolved object, never rebuilding them from the name itself.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Variant:
    """A fully-resolved worktree variant (tenant, language, snapshot, creds).

    ``canonical_tenant`` collapses an alias variant onto its parent's tenant
    (a child variant whose data is identical to its parent shares snapshots),
    which is why it is a resolved field rather than something a caller derives
    from ``name`` with a prefix rule.
    """

    name: str
    canonical_tenant: str
    default_language: str = ""
    dslr_snapshot_name: str = ""
    e2e_credentials_key: str = ""

    @classmethod
    def bare(cls, name: str) -> "Variant":
        """A pass-through variant whose tenant is the name verbatim.

        The default an overlay with no per-tenant prefix / alias / snapshot
        scheme resolves to: the tenant *is* the variant name, with no DSLR
        snapshot or E2E credentials key derived. An empty ``name`` yields the
        empty variant (``canonical_tenant == ""``), which callers filter out.
        """
        return cls(name=name, canonical_tenant=name)
