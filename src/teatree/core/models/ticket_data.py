from typing import TYPE_CHECKING, Any, ClassVar

from django.db import models

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


class TicketFacet(models.Model):
    """Field-less abstract base carrying the type surface the ``Ticket`` facets share.

    The concrete ``Ticket`` supplies the real fields/enums; each behaviour facet
    (overlay attribution, phase sessions, scheduling, evidence, introspection)
    subclasses this so its methods type-check against the model's fields without
    redeclaring them. Abstract with no fields, so it contributes no migration and
    the multiple-inheritance diamond into ``Ticket`` cannot clash.
    """

    class Meta:
        abstract = True

    if TYPE_CHECKING:
        pk: int
        issue_url: str
        overlay: str
        state: str
        role: str
        extra: dict[str, Any]
        context: str
        remote_missing: bool
        expedited: bool
        issue_number: str
        State: type["Ticket.State"]
        Role: type["Ticket.Role"]
        _TERMINAL_STATES: ClassVar[frozenset[str]]
