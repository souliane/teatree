from teatree.core.modelkit.gate_registry import get_resolver
from teatree.core.models.ticket_data import TicketFacet


class TicketOverlayModel(TicketFacet):
    """Overlay attribution for a ticket: infer it from ``issue_url`` and keep it current."""

    class Meta:
        abstract = True

    def _infer_overlay(self) -> str:
        """Derive overlay name from ``issue_url`` (see ``infer_overlay_for_url``)."""
        return str(get_resolver("infer_overlay_for_url")(self.issue_url))

    def apply_inferred_overlay(self, inferred: str) -> bool:
        """Persist ``inferred`` overlay on a conclusive change (True when changed).

        A blank inference never blanks out a manually-set attribution.
        """
        if not inferred or inferred == self.overlay:
            return False
        self.overlay = inferred
        type(self).objects.filter(pk=self.pk).update(overlay=inferred)
        return True

    def reconcile_overlay(self) -> bool:
        """Re-infer ``overlay`` from ``issue_url`` and persist a correction."""
        return self.apply_inferred_overlay(self._infer_overlay())

    def has_dispatchable_overlay(self) -> bool:
        """False only for a non-empty overlay that no longer resolves (#1959 poison-pill)."""
        return not (self.overlay and get_resolver("resolve_overlay_name")(self.overlay) is None)
