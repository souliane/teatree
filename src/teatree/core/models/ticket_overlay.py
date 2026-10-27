from collections.abc import Iterable

from django.db import models

from teatree.core.modelkit.gate_registry import get_resolver
from teatree.core.models.operator_identity import canonical_operator_key
from teatree.core.models.ticket_data import TicketFacet
from teatree.utils.url_slug import is_synthetic_loop_umbrella_url


class TicketOverlayModel(TicketFacet):
    """Overlay attribution for a ticket: infer it from ``issue_url`` and keep it current."""

    requested_by = models.CharField(max_length=180, blank=True, default="", db_index=True)

    class Meta:
        abstract = True

    def save(
        self,
        force_insert: bool = False,  # noqa: FBT001, FBT002 — Django's save accepts these positionally.
        force_update: bool = False,  # noqa: FBT001, FBT002 — Django's save accepts these positionally.
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        if self.pk is not None and update_fields is None:
            recorded = type(self).objects.filter(pk=self.pk).values_list("requested_by", flat=True).first()
            if recorded:
                self.requested_by = recorded
        super().save(force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields)

    def record_requester(self, key: str) -> bool:
        canonical = canonical_operator_key(key)
        if self.pk is None:
            return False
        updated = type(self).objects.filter(pk=self.pk, requested_by="").update(requested_by=canonical)
        if updated:
            self.requested_by = canonical
        return bool(updated)

    def _infer_overlay(self) -> str:
        """Derive overlay name from ``issue_url`` (see ``infer_overlay_for_url``)."""
        return str(get_resolver("infer_overlay_for_url")(self.issue_url))

    @staticmethod
    def creation_overlay(scope_overlay: str) -> str:
        return scope_overlay or str(get_resolver("running_checkout_overlay")())

    def would_apply_inferred_overlay(self, inferred: str) -> bool:
        return bool(inferred and inferred != self.overlay and not is_synthetic_loop_umbrella_url(self.issue_url))

    def apply_inferred_overlay(self, inferred: str) -> bool:
        """Persist ``inferred`` overlay on a conclusive change (True when changed).

        A blank inference never blanks out a manually-set attribution.
        """
        if not self.would_apply_inferred_overlay(inferred):
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
